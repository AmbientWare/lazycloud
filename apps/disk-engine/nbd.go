package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
)

// A read-only NBD server with the fixed-newstyle handshake, simple replies and
// the READ, FLUSH and DISC commands: what qemu's nbd block driver needs to open
// an export as a backing file's protocol node. Options it does not implement,
// structured replies and metadata contexts among them, are refused, and qemu
// carries on without them.
const (
	nbdMagic            uint64 = 0x4e42444d41474943 // "NBDMAGIC"
	nbdOptionMagic      uint64 = 0x49484156454f5054 // "IHAVEOPT"
	nbdReplyMagic       uint64 = 0x0003e889045565a9
	nbdRequestMagic     uint32 = 0x25609513
	nbdSimpleReplyMagic uint32 = 0x67446698

	nbdFlagFixedNewstyle uint16 = 1 << 0
	nbdFlagNoZeroes      uint16 = 1 << 1
	nbdClientNoZeroes    uint32 = 1 << 1

	nbdOptExportName uint32 = 1
	nbdOptAbort      uint32 = 2
	nbdOptInfo       uint32 = 6
	nbdOptGo         uint32 = 7

	nbdRepAck        uint32 = 1
	nbdRepInfo       uint32 = 3
	nbdRepErrUnsup   uint32 = 1<<31 + 1
	nbdRepErrInvalid uint32 = 1<<31 + 3
	nbdRepErrUnknown uint32 = 1<<31 + 6

	nbdInfoExport    uint16 = 0
	nbdInfoBlockSize uint16 = 3

	nbdTransHasFlags  uint16 = 1 << 0
	nbdTransReadOnly  uint16 = 1 << 1
	nbdTransSendFlush uint16 = 1 << 2

	nbdCmdRead  uint16 = 0
	nbdCmdWrite uint16 = 1
	nbdCmdDisc  uint16 = 2
	nbdCmdFlush uint16 = 3

	nbdEPERM  uint32 = 1
	nbdEIO    uint32 = 5
	nbdEINVAL uint32 = 22

	nbdMaxRequest     = 32 << 20
	nbdMaxOptionBytes = 64 << 10
	nbdInflight       = 64
)

type nbdExport interface {
	Size() int64
	ReadAt(ctx context.Context, buf []byte, offset int64) error
}

type nbdServer struct {
	exports map[string]nbdExport
	// failed hears about every read that returned an error to the client.
	failed func(export string, err error)
}

func (s *nbdServer) serve(ctx context.Context, listener net.Listener) error {
	go func() {
		<-ctx.Done()
		listener.Close()
	}()
	var wg sync.WaitGroup
	defer wg.Wait()
	for {
		conn, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer conn.Close()
			s.session(ctx, conn)
		}()
	}
}

func (s *nbdServer) session(ctx context.Context, conn net.Conn) {
	go func() {
		<-ctx.Done()
		conn.Close()
	}()
	reader := bufio.NewReader(conn)
	name, export, err := s.negotiate(reader, conn)
	if err != nil || export == nil {
		return
	}
	s.transmit(ctx, reader, conn, name, export)
}

func (s *nbdServer) negotiate(reader io.Reader, conn io.Writer) (string, nbdExport, error) {
	greeting := binary.BigEndian.AppendUint64(nil, nbdMagic)
	greeting = binary.BigEndian.AppendUint64(greeting, nbdOptionMagic)
	greeting = binary.BigEndian.AppendUint16(greeting, nbdFlagFixedNewstyle|nbdFlagNoZeroes)
	if _, err := conn.Write(greeting); err != nil {
		return "", nil, err
	}
	var clientFlags uint32
	if err := binary.Read(reader, binary.BigEndian, &clientFlags); err != nil {
		return "", nil, err
	}
	for {
		var header struct {
			Magic  uint64
			Option uint32
			Length uint32
		}
		if err := binary.Read(reader, binary.BigEndian, &header); err != nil {
			return "", nil, err
		}
		if header.Magic != nbdOptionMagic || header.Length > nbdMaxOptionBytes {
			return "", nil, errors.New("nbd: malformed option")
		}
		data := make([]byte, header.Length)
		if _, err := io.ReadFull(reader, data); err != nil {
			return "", nil, err
		}
		switch header.Option {
		case nbdOptExportName:
			name := string(data)
			export := s.exports[name]
			if export == nil {
				return "", nil, fmt.Errorf("nbd: no export %q", name)
			}
			reply := binary.BigEndian.AppendUint64(nil, uint64(export.Size()))
			reply = binary.BigEndian.AppendUint16(reply, nbdTransHasFlags|nbdTransReadOnly|nbdTransSendFlush)
			if clientFlags&nbdClientNoZeroes == 0 {
				reply = append(reply, make([]byte, 124)...)
			}
			_, err := conn.Write(reply)
			return name, export, err
		case nbdOptInfo, nbdOptGo:
			if len(data) < 6 {
				if err := optionReply(conn, header.Option, nbdRepErrInvalid, nil); err != nil {
					return "", nil, err
				}
				continue
			}
			length := binary.BigEndian.Uint32(data)
			if int(length)+6 > len(data) {
				if err := optionReply(conn, header.Option, nbdRepErrInvalid, nil); err != nil {
					return "", nil, err
				}
				continue
			}
			name := string(data[4 : 4+length])
			export := s.exports[name]
			if export == nil {
				if err := optionReply(conn, header.Option, nbdRepErrUnknown, nil); err != nil {
					return "", nil, err
				}
				continue
			}
			info := binary.BigEndian.AppendUint16(nil, nbdInfoExport)
			info = binary.BigEndian.AppendUint64(info, uint64(export.Size()))
			info = binary.BigEndian.AppendUint16(info, nbdTransHasFlags|nbdTransReadOnly|nbdTransSendFlush)
			sizes := binary.BigEndian.AppendUint16(nil, nbdInfoBlockSize)
			sizes = binary.BigEndian.AppendUint32(sizes, 1)
			sizes = binary.BigEndian.AppendUint32(sizes, 4096)
			sizes = binary.BigEndian.AppendUint32(sizes, nbdMaxRequest)
			for _, reply := range [][]byte{info, sizes} {
				if err := optionReply(conn, header.Option, nbdRepInfo, reply); err != nil {
					return "", nil, err
				}
			}
			if err := optionReply(conn, header.Option, nbdRepAck, nil); err != nil {
				return "", nil, err
			}
			if header.Option == nbdOptGo {
				return name, export, nil
			}
		case nbdOptAbort:
			optionReply(conn, header.Option, nbdRepAck, nil)
			return "", nil, nil
		default:
			if err := optionReply(conn, header.Option, nbdRepErrUnsup, nil); err != nil {
				return "", nil, err
			}
		}
	}
}

func optionReply(conn io.Writer, option, kind uint32, data []byte) error {
	reply := binary.BigEndian.AppendUint64(nil, nbdReplyMagic)
	reply = binary.BigEndian.AppendUint32(reply, option)
	reply = binary.BigEndian.AppendUint32(reply, kind)
	reply = binary.BigEndian.AppendUint32(reply, uint32(len(data)))
	_, err := conn.Write(append(reply, data...))
	return err
}

func (s *nbdServer) transmit(ctx context.Context, reader io.Reader, conn io.Writer, name string, export nbdExport) {
	var writeMu sync.Mutex
	reply := func(cookie uint64, code uint32, data []byte) error {
		header := binary.BigEndian.AppendUint32(nil, nbdSimpleReplyMagic)
		header = binary.BigEndian.AppendUint32(header, code)
		header = binary.BigEndian.AppendUint64(header, cookie)
		writeMu.Lock()
		defer writeMu.Unlock()
		if _, err := conn.Write(header); err != nil {
			return err
		}
		if len(data) > 0 {
			_, err := conn.Write(data)
			return err
		}
		return nil
	}
	slots := make(chan struct{}, nbdInflight)
	var wg sync.WaitGroup
	defer wg.Wait()
	for {
		var request struct {
			Magic  uint32
			Flags  uint16
			Type   uint16
			Cookie uint64
			Offset uint64
			Length uint32
		}
		if err := binary.Read(reader, binary.BigEndian, &request); err != nil {
			return
		}
		if request.Magic != nbdRequestMagic {
			return
		}
		switch request.Type {
		case nbdCmdRead:
			if request.Length > nbdMaxRequest || request.Offset+uint64(request.Length) > uint64(export.Size()) {
				if reply(request.Cookie, nbdEINVAL, nil) != nil {
					return
				}
				continue
			}
			slots <- struct{}{}
			wg.Add(1)
			go func() {
				defer wg.Done()
				defer func() { <-slots }()
				buf := make([]byte, request.Length)
				if err := export.ReadAt(ctx, buf, int64(request.Offset)); err != nil {
					if s.failed != nil {
						s.failed(name, err)
					}
					reply(request.Cookie, nbdEIO, nil)
					return
				}
				reply(request.Cookie, 0, buf)
			}()
		case nbdCmdWrite:
			// A read-only export takes no writes, but the payload follows the
			// request and has to be consumed to keep the stream aligned.
			if request.Length > nbdMaxRequest {
				return
			}
			if _, err := io.CopyN(io.Discard, reader, int64(request.Length)); err != nil {
				return
			}
			if reply(request.Cookie, nbdEPERM, nil) != nil {
				return
			}
		case nbdCmdFlush:
			if reply(request.Cookie, 0, nil) != nil {
				return
			}
		case nbdCmdDisc:
			return
		default:
			if reply(request.Cookie, nbdEINVAL, nil) != nil {
				return
			}
		}
	}
}

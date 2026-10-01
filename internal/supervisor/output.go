package supervisor

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"golang.org/x/sys/unix"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	readChunk = 32 << 10
	// drainLimit bounds one synchronous drain, so a runner that keeps writing
	// cannot hold a slot's output lock.
	drainLimit = 256 << 10
)

// outputPipe is the read end of a runner's stdout or stderr. Reads happen
// under the slot's output lock, which is also held while the slot switches
// attempts, so every chunk carries the attempt that was current when it left
// the pipe. The runner flushes before it replies, so an attempt's output is
// in the pipe before its result is in the socket.
type outputPipe struct {
	file    *os.File
	raw     syscall.RawConn
	stream  hostproto.LogStream
	pending []byte
	// partial is the start of a line not yet ended.
	partial string
}

// partialWait is how long the start of a line waits for its end before it
// is emitted alone, so a prompt or progress output still arrives.
const partialWait = 50 * time.Millisecond

func newOutputPipe(file *os.File, stream hostproto.LogStream) (*outputPipe, error) {
	raw, err := file.SyscallConn()
	if err != nil {
		return nil, fmt.Errorf("open output pipe: %w", err)
	}
	return &outputPipe{file: file, raw: raw, stream: stream}, nil
}

// readOutput copies a pipe to the outbox until the pipe closes. It waits for
// outbox space between reads, which pushes back on the runner.
func (sl *slot) readOutput(ctx context.Context, pipe *outputPipe) {
	for {
		var done bool
		sl.outMu.Lock()
		waiting := pipe.partial != ""
		sl.outMu.Unlock()
		deadline := time.Time{}
		if waiting {
			deadline = time.Now().Add(partialWait)
		}
		_ = pipe.file.SetReadDeadline(deadline)
		err := pipe.raw.Read(func(fd uintptr) bool {
			sl.outMu.Lock()
			defer sl.outMu.Unlock()
			n, err := unix.Read(int(fd), sl.buf)
			if errors.Is(err, unix.EAGAIN) || errors.Is(err, unix.EINTR) {
				return false
			}
			if n > 0 {
				sl.emitLocked(pipe, sl.buf[:n], false)
			} else {
				done = true
			}
			return true
		})
		if errors.Is(err, os.ErrDeadlineExceeded) {
			sl.outMu.Lock()
			sl.emitPartialLocked(pipe)
			sl.outMu.Unlock()
			continue
		}
		if err != nil || done {
			return
		}
		if sl.sup.out.waitOutputSpace(ctx) != nil {
			return
		}
	}
}

// drainLocked emits what the current runner's pipes hold now. flush also
// emits an incomplete UTF-8 sequence, which happens when an attempt ends.
func (sl *slot) drainLocked(flush bool) {
	if sl.proc == nil {
		return
	}
	for _, pipe := range sl.proc.pipes {
		_ = pipe.raw.Control(func(fd uintptr) {
			for total := 0; total < drainLimit; {
				n, err := unix.Read(int(fd), sl.buf)
				if n <= 0 || err != nil {
					return
				}
				total += n
				sl.emitLocked(pipe, sl.buf[:n], false)
			}
		})
		if flush {
			sl.emitLocked(pipe, nil, true)
		}
	}
}

// emitLocked sends each completed line of data without its newline, and
// with flush the start of a line too, which happens when an attempt ends.
func (sl *slot) emitLocked(pipe *outputPipe, data []byte, flush bool) {
	text := pipe.partial + pipe.decode(data, flush)
	pipe.partial = ""
	for {
		line, rest, found := strings.Cut(text, "\n")
		if !found {
			pipe.partial = text
			break
		}
		sl.pushLineLocked(pipe, line)
		text = rest
	}
	if flush {
		sl.emitPartialLocked(pipe)
	}
}

func (sl *slot) emitPartialLocked(pipe *outputPipe) {
	if pipe.partial != "" {
		sl.pushLineLocked(pipe, pipe.partial)
		pipe.partial = ""
	}
}

func (sl *slot) pushLineLocked(pipe *outputPipe, line string) {
	sl.sup.out.push(&hostproto.SupervisorMessage{Body: &hostproto.SupervisorMessage_Output{Output: &hostproto.OutputChunk{
		AttemptId: sl.attempt,
		Stream:    pipe.stream,
		Data:      line,
		Time:      timestamppb.Now(),
	}}})
}

// decode returns data as valid UTF-8. It holds back an incomplete trailing
// sequence for the next read unless flush is set.
func (p *outputPipe) decode(data []byte, flush bool) string {
	buf := append(p.pending, data...)
	cut := len(buf)
	if !flush {
		cut = completeUTF8(buf)
	}
	p.pending = append([]byte(nil), buf[cut:]...)
	return strings.ToValidUTF8(string(buf[:cut]), "�")
}

// completeUTF8 returns the length of b without an incomplete final rune.
func completeUTF8(b []byte) int {
	for i := len(b) - 1; i >= 0 && i >= len(b)-utf8.UTFMax; i-- {
		if utf8.RuneStart(b[i]) {
			if utf8.FullRune(b[i:]) {
				return len(b)
			}
			return i
		}
	}
	return len(b)
}

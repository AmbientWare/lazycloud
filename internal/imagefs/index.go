package imagefs

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"path"
	"strings"
	"time"

	"github.com/klauspost/compress/zstd"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

const (
	indexMagic = "LCIX"
	// formatVersion is the only stored format this package reads and writes.
	formatVersion uint32 = 1
	headerSize           = len(indexMagic) + 4
	// maxIndexSize bounds a decoded index. The index of a 1 GiB torch layer
	// with 26,516 entries decodes to 3.0 MB.
	maxIndexSize = 64 << 20
	// maxEntries and maxFrames bound the records Unmarshal allocates, which
	// an index of empty records could otherwise multiply far past its size.
	// maxFrames covers a 256 GiB layer.
	maxEntries = 2 << 20
	maxFrames  = 1 << 16
)

// Marshal encodes the index in the stored format: a version header, then
// the protobuf encoding compressed as one zstd frame. Equal indexes encode
// to equal bytes.
func (ix Index) Marshal() ([]byte, error) {
	msg := &imagefsproto.Index{
		Layer:      string(ix.Layer),
		StreamSize: ix.StreamSize,
		FrameSizes: make([]int64, len(ix.Frames)),
		Entries:    make([]*imagefsproto.Entry, len(ix.Entries)),
	}
	for i, f := range ix.Frames {
		msg.FrameSizes[i] = f.Size
	}
	for i, e := range ix.Entries {
		t, err := entryTypeProto(e.Type)
		if err != nil {
			return nil, err
		}
		msg.Entries[i] = &imagefsproto.Entry{
			Path:           e.Path,
			Type:           t,
			Mode:           posixMode(e.Mode),
			Uid:            e.UID,
			Gid:            e.GID,
			ModTimeSeconds: e.ModTime.Unix(),
			ModTimeNanos:   int32(e.ModTime.Nanosecond()), //nolint:gosec // Below 1e9.
			LinkTarget:     e.LinkTarget,
			DevMajor:       e.DevMajor,
			DevMinor:       e.DevMinor,
			Xattrs:         e.Xattrs,
			Whiteout:       e.Whiteout,
			Opaque:         e.Opaque,
			Offset:         e.Offset,
			Size:           e.Size,
		}
	}
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(msg)
	if err != nil {
		return nil, fmt.Errorf("encode index: %w", err)
	}
	enc, err := zstd.NewWriter(nil, zstd.WithEncoderConcurrency(1), zstd.WithEncoderCRC(true))
	if err != nil {
		return nil, fmt.Errorf("start index encoder: %w", err)
	}
	defer func() { _ = enc.Close() }()
	out := make([]byte, headerSize, headerSize+len(raw)/4)
	copy(out, indexMagic)
	binary.BigEndian.PutUint32(out[len(indexMagic):], formatVersion)
	return enc.EncodeAll(raw, out), nil
}

// Unmarshal decodes a stored index. It refuses an unknown format version
// with ErrUnsupportedVersion and an index that breaks the format's
// invariants with ErrInvalidIndex.
func Unmarshal(b []byte) (Index, error) {
	if len(b) < headerSize || string(b[:len(indexMagic)]) != indexMagic {
		return Index{}, fmt.Errorf("%w: no index header", ErrInvalidIndex)
	}
	if v := binary.BigEndian.Uint32(b[len(indexMagic):headerSize]); v != formatVersion {
		return Index{}, fmt.Errorf("%w: %d, this reader knows %d", ErrUnsupportedVersion, v, formatVersion)
	}
	dec, err := zstd.NewReader(nil, zstd.WithDecoderConcurrency(1), zstd.WithDecoderMaxMemory(maxIndexSize), zstd.IgnoreChecksum(false))
	if err != nil {
		return Index{}, fmt.Errorf("start index decoder: %w", err)
	}
	defer dec.Close()
	raw, err := dec.DecodeAll(b[headerSize:], nil)
	if err != nil {
		return Index{}, fmt.Errorf("%w: decompress: %w", ErrInvalidIndex, err)
	}
	if err := checkRecordCounts(raw); err != nil {
		return Index{}, err
	}
	var msg imagefsproto.Index
	if err := proto.Unmarshal(raw, &msg); err != nil {
		return Index{}, fmt.Errorf("%w: decode: %w", ErrInvalidIndex, err)
	}
	ix := Index{Layer: Digest(msg.GetLayer()), StreamSize: msg.GetStreamSize()}
	for _, size := range msg.GetFrameSizes() {
		ix.Frames = append(ix.Frames, Frame{Offset: ix.DataSize, Size: size})
		ix.DataSize += size
	}
	if n := len(msg.GetEntries()); n > 0 {
		ix.Entries = make([]Entry, n)
	}
	for i, m := range msg.GetEntries() {
		t, err := entryType(m.GetType())
		if err != nil {
			return Index{}, fmt.Errorf("%w: %s: %w", ErrInvalidIndex, m.GetPath(), err)
		}
		if m.GetMode() > 0o7777 || m.GetModTimeNanos() < 0 || m.GetModTimeNanos() >= 1e9 {
			return Index{}, fmt.Errorf("%w: %s has mode %o, nanoseconds %d", ErrInvalidIndex, m.GetPath(), m.GetMode(), m.GetModTimeNanos())
		}
		ix.Entries[i] = Entry{
			Path:       m.GetPath(),
			Type:       t,
			Mode:       fileMode(t, m.GetMode()),
			UID:        m.GetUid(),
			GID:        m.GetGid(),
			ModTime:    time.Unix(m.GetModTimeSeconds(), int64(m.GetModTimeNanos())).UTC(),
			LinkTarget: m.GetLinkTarget(),
			DevMajor:   m.GetDevMajor(),
			DevMinor:   m.GetDevMinor(),
			Xattrs:     m.GetXattrs(),
			Whiteout:   m.GetWhiteout(),
			Opaque:     m.GetOpaque(),
			Offset:     m.GetOffset(),
			Size:       m.GetSize(),
		}
	}
	if err := ix.validate(); err != nil {
		return Index{}, err
	}
	return ix, nil
}

// checkRecordCounts counts the entries and frames of an encoded index
// before it is decoded.
func checkRecordCounts(raw []byte) error {
	fields := (&imagefsproto.Index{}).ProtoReflect().Descriptor().Fields()
	entriesField := fields.ByName("entries").Number()
	framesField := fields.ByName("frame_sizes").Number()
	var entries, frames int
	for len(raw) > 0 {
		num, typ, n := protowire.ConsumeTag(raw)
		if n < 0 {
			return fmt.Errorf("%w: decode: %w", ErrInvalidIndex, protowire.ParseError(n))
		}
		raw = raw[n:]
		n = protowire.ConsumeFieldValue(num, typ, raw)
		if n < 0 {
			return fmt.Errorf("%w: decode: %w", ErrInvalidIndex, protowire.ParseError(n))
		}
		switch {
		case num == entriesField:
			entries++
		case num == framesField && typ == protowire.BytesType:
			packed, _ := protowire.ConsumeBytes(raw)
			for _, b := range packed {
				if b < 0x80 {
					frames++
				}
			}
		case num == framesField:
			frames++
		}
		raw = raw[n:]
	}
	if entries > maxEntries || frames > maxFrames {
		return fmt.Errorf("%w: %d entries and %d frames, at most %d and %d", ErrInvalidIndex, entries, frames, maxEntries, maxFrames)
	}
	return nil
}

// validate checks what readers rely on: the frame table covers the stream,
// every file's bytes lie inside it, and the tree has one entry per path
// with each directory before its children.
func (ix Index) validate() error {
	if hexDigest, ok := strings.CutPrefix(string(ix.Layer), "sha256:"); !ok || len(hexDigest) != 64 || strings.ToLower(hexDigest) != hexDigest || !isHex(hexDigest) {
		return fmt.Errorf("%w: layer digest %q", ErrInvalidIndex, ix.Layer)
	}
	if ix.StreamSize < 0 || int64(len(ix.Frames)) != (ix.StreamSize+FrameSize-1)/FrameSize {
		return fmt.Errorf("%w: %d frames for a %d byte stream", ErrInvalidIndex, len(ix.Frames), ix.StreamSize)
	}
	var offset int64
	for i, f := range ix.Frames {
		if f.Size <= 0 || f.Size > maxPackedFrame || f.Offset != offset {
			return fmt.Errorf("%w: frame %d at %d of %d bytes", ErrInvalidIndex, i, f.Offset, f.Size)
		}
		offset += f.Size
	}
	if offset != ix.DataSize {
		return fmt.Errorf("%w: frames hold %d of %d data bytes", ErrInvalidIndex, offset, ix.DataSize)
	}
	seen := make(map[string]int, len(ix.Entries))
	for i, e := range ix.Entries {
		if err := ix.validateEntry(e, seen); err != nil {
			return fmt.Errorf("%w: %q: %w", ErrInvalidIndex, e.Path, err)
		}
		seen[e.Path] = i
	}
	return nil
}

// validateEntry checks e against the entries before it, which seen maps
// from path to index.
func (ix Index) validateEntry(e Entry, seen map[string]int) error {
	if e.Path == "" || cleanPath(e.Path) != e.Path {
		return fmt.Errorf("path is not clean")
	}
	if _, ok := seen[e.Path]; ok {
		return fmt.Errorf("path repeats")
	}
	if parent := path.Dir(e.Path); e.Path != "." && parent != "." {
		if i, ok := seen[parent]; !ok || ix.Entries[i].Type != TypeDirectory {
			return fmt.Errorf("no directory %q before it", parent)
		}
	}
	if e.Path == "." && e.Type != TypeDirectory {
		return fmt.Errorf("root is a %s", e.Type)
	}
	hasBytes := e.Type == TypeRegular || e.Type == TypeHardLink
	if e.Offset < 0 || e.Size < 0 || e.Offset > ix.StreamSize-e.Size || !hasBytes && e.Offset|e.Size != 0 {
		return fmt.Errorf("bytes [%d, +%d) outside a %d byte stream", e.Offset, e.Size, ix.StreamSize)
	}
	if e.Type == TypeHardLink {
		i, ok := seen[e.LinkTarget]
		if !ok {
			return fmt.Errorf("hard link to missing %q", e.LinkTarget)
		}
		if t := ix.Entries[i]; t.Type == TypeDirectory || t.Type == TypeHardLink || t.Whiteout || t.Offset != e.Offset || t.Size != e.Size {
			return fmt.Errorf("hard link to %s %q of bytes [%d, +%d)", t.Type, t.Path, t.Offset, t.Size)
		}
	}
	if e.Whiteout && (e.Type != TypeCharDevice || e.DevMajor|e.DevMinor != 0) || e.Opaque && e.Type != TypeDirectory {
		return fmt.Errorf("whiteout or opaque marker on a %s", e.Type)
	}
	return nil
}

func entryTypeProto(t EntryType) (imagefsproto.EntryType, error) {
	switch t {
	case TypeRegular:
		return imagefsproto.EntryType_ENTRY_TYPE_REGULAR, nil
	case TypeDirectory:
		return imagefsproto.EntryType_ENTRY_TYPE_DIRECTORY, nil
	case TypeSymlink:
		return imagefsproto.EntryType_ENTRY_TYPE_SYMLINK, nil
	case TypeHardLink:
		return imagefsproto.EntryType_ENTRY_TYPE_HARD_LINK, nil
	case TypeCharDevice:
		return imagefsproto.EntryType_ENTRY_TYPE_CHAR_DEVICE, nil
	case TypeBlockDevice:
		return imagefsproto.EntryType_ENTRY_TYPE_BLOCK_DEVICE, nil
	case TypeFIFO:
		return imagefsproto.EntryType_ENTRY_TYPE_FIFO, nil
	}
	return 0, fmt.Errorf("%w: entry type %q", ErrInvalidIndex, t)
}

func entryType(t imagefsproto.EntryType) (EntryType, error) {
	switch t {
	case imagefsproto.EntryType_ENTRY_TYPE_REGULAR:
		return TypeRegular, nil
	case imagefsproto.EntryType_ENTRY_TYPE_DIRECTORY:
		return TypeDirectory, nil
	case imagefsproto.EntryType_ENTRY_TYPE_SYMLINK:
		return TypeSymlink, nil
	case imagefsproto.EntryType_ENTRY_TYPE_HARD_LINK:
		return TypeHardLink, nil
	case imagefsproto.EntryType_ENTRY_TYPE_CHAR_DEVICE:
		return TypeCharDevice, nil
	case imagefsproto.EntryType_ENTRY_TYPE_BLOCK_DEVICE:
		return TypeBlockDevice, nil
	case imagefsproto.EntryType_ENTRY_TYPE_FIFO:
		return TypeFIFO, nil
	case imagefsproto.EntryType_ENTRY_TYPE_UNSPECIFIED:
	}
	return "", fmt.Errorf("entry type %d", t)
}

// frameLen is the uncompressed length of frame i.
func (ix Index) frameLen(i int) int {
	if i == len(ix.Frames)-1 {
		if rest := ix.StreamSize % FrameSize; rest != 0 {
			return int(rest)
		}
	}
	return FrameSize
}

func isHex(s string) bool {
	_, err := hex.DecodeString(s)
	return err == nil
}

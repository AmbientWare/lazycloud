package imagefs

import (
	"archive/tar"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"math"
	"os"
	"path"
	"strings"
	"time"

	"github.com/klauspost/compress/zstd"
)

const (
	whiteoutPrefix = ".wh."
	// Names under this prefix are whiteout metadata. Only the opaque marker
	// has an OCI meaning; the others are skipped.
	whiteoutMetaPrefix = ".wh..wh."
	opaqueMarker       = ".wh..wh..opq"
	xattrPrefix        = "SCHILY.xattr."
)

// ConvertedFile is a converted layer: its data object in a file and its
// encoded index.
type ConvertedFile struct {
	Path      string
	DataBytes int64
	Index     []byte
	Entries   int // paths in the index
}

// ConvertFile converts layer, which the image config names diffID, into a
// new data file under dir. A layer Convert refuses or whose content is not
// diffID is ErrInvalidLayer. A failed conversion leaves no file.
func ConvertFile(ctx context.Context, layer io.Reader, dir string, diffID Digest) (out *ConvertedFile, err error) {
	data, err := os.CreateTemp(dir, "layer-*.data")
	if err != nil {
		return nil, fmt.Errorf("create layer data file: %w", err)
	}
	defer func() {
		if closeErr := data.Close(); closeErr != nil && err == nil {
			err = fmt.Errorf("write layer data file: %w", closeErr)
		}
		if err != nil {
			out, err = nil, errors.Join(err, os.Remove(data.Name()))
		}
	}()
	ix, err := Convert(ctx, layer, data)
	if err != nil {
		return nil, err
	}
	if ix.Layer != diffID {
		return nil, fmt.Errorf("%w: its content is %s, but the image config names %s", ErrInvalidLayer, ix.Layer, diffID)
	}
	index, err := ix.Marshal()
	if err != nil {
		return nil, err
	}
	return &ConvertedFile{Path: data.Name(), DataBytes: ix.DataSize, Index: index, Entries: len(ix.Entries)}, nil
}

// Convert reads an uncompressed OCI layer tar, writes the compressed data
// object to data as it goes, and returns the index. Memory stays bounded
// by a few frames whatever the layer size.
//
// Entries keep tar order with one entry per path: a later entry replaces an
// earlier one in place, and a non-directory replacing a directory removes
// what was below it. A layer that replaces or removes an entry a hard link
// names fails with ErrInvalidLayer. Missing parent directories are added as
// 0755 root-owned directories just before their first child. PAX global
// headers describe no file and are skipped, as extraction skips them.
func Convert(ctx context.Context, layer io.Reader, data io.Writer) (Index, error) {
	enc, err := zstd.NewWriter(nil, zstd.WithEncoderConcurrency(1), zstd.WithEncoderCRC(true),
		zstd.WithWindowSize(FrameSize), zstd.WithLowerEncoderMem(true))
	if err != nil {
		return Index{}, fmt.Errorf("start frame encoder: %w", err)
	}
	defer func() { _ = enc.Close() }()
	hash := sha256.New()
	in := io.TeeReader(layer, hash)
	c := &converter{
		ctx:    ctx,
		enc:    enc,
		out:    data,
		frame:  make([]byte, 0, FrameSize),
		byPath: map[string]int{},
		links:  map[string]int{},
	}
	tr := tar.NewReader(in)
	for {
		if err := ctx.Err(); err != nil {
			return Index{}, fmt.Errorf("convert layer: %w", err)
		}
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return Index{}, tarError(err)
		}
		if hdr.Typeflag == tar.TypeXGlobalHeader {
			continue
		}
		if err := c.add(hdr, tr); err != nil {
			return Index{}, err
		}
	}
	if err := c.flush(); err != nil {
		return Index{}, err
	}
	// The diff_id covers the whole stream, including the tar's end padding.
	if _, err := io.Copy(io.Discard, in); err != nil {
		return Index{}, tarError(err)
	}
	entries := c.entries[:0]
	for _, e := range c.entries {
		if e.Path != "" {
			entries = append(entries, e)
		}
	}
	ix := Index{
		Layer:      Digest("sha256:" + hex.EncodeToString(hash.Sum(nil))),
		StreamSize: c.streamSize,
		DataSize:   c.dataSize,
		Frames:     c.frames,
		Entries:    entries,
	}
	if err := ix.validate(); err != nil {
		return Index{}, fmt.Errorf("%w: %w", ErrInvalidLayer, err)
	}
	return ix, nil
}

// tarError is a failure reading the layer: ErrInvalidLayer when the tar
// stream is truncated or malformed, which reading it again does not change.
func tarError(err error) error {
	if errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, tar.ErrHeader) || errors.Is(err, tar.ErrFieldTooLong) {
		return fmt.Errorf("%w: %w", ErrInvalidLayer, err)
	}
	return fmt.Errorf("read layer tar: %w", err)
}

type converter struct {
	ctx        context.Context
	enc        *zstd.Encoder
	out        io.Writer
	frame      []byte // the current frame's uncompressed bytes
	packed     []byte // the last compressed frame, reused
	streamSize int64
	dataSize   int64
	frames     []Frame
	// entries holds removed entries with an empty Path until Convert
	// compacts it, so byPath indexes stay valid.
	entries []Entry
	byPath  map[string]int
	links   map[string]int // hard links naming each path
}

func (c *converter) add(hdr *tar.Header, r io.Reader) error {
	p := cleanPath(hdr.Name)
	base := path.Base(p)
	if base == opaqueMarker {
		i, err := c.dir(path.Dir(p), true)
		if err != nil {
			return err
		}
		c.entries[i].Opaque = true
		return nil
	}
	if strings.HasPrefix(base, whiteoutMetaPrefix) {
		return nil
	}
	e, err := headerEntry(p, hdr)
	if err != nil {
		return err
	}
	if name, ok := strings.CutPrefix(base, whiteoutPrefix); ok && name != "" {
		return c.put(Entry{
			Path: path.Join(path.Dir(p), name), Type: TypeCharDevice, Whiteout: true,
			Mode: fileMode(TypeCharDevice, uint32(hdr.Mode&07777)), //nolint:gosec // Masked to 12 bits.
			UID:  e.UID, GID: e.GID, ModTime: e.ModTime,
		})
	}
	switch hdr.Typeflag {
	case tar.TypeReg, tar.TypeGNUSparse: // archive/tar fills a sparse file's holes
		e.Type = TypeRegular
		if hdr.Size > 0 {
			if e.Offset, err = c.write(r, hdr.Size); err != nil {
				return fmt.Errorf("read %s: %w", p, err)
			}
			e.Size = hdr.Size
		}
	case tar.TypeDir:
		e.Type = TypeDirectory
	case tar.TypeSymlink:
		e.Type, e.LinkTarget = TypeSymlink, hdr.Linkname
	case tar.TypeLink:
		target, ok := c.byPath[cleanPath(hdr.Linkname)]
		if !ok {
			return fmt.Errorf("%w: hard link %s to missing %s", ErrInvalidLayer, p, hdr.Linkname)
		}
		t := c.entries[target]
		if t.Type == TypeHardLink {
			if target, ok = c.byPath[t.LinkTarget]; ok {
				t = c.entries[target]
			}
		}
		if !ok || t.Type == TypeDirectory || t.Type == TypeHardLink || t.Whiteout || t.Path == p {
			return fmt.Errorf("%w: hard link %s to %s", ErrInvalidLayer, p, t.Path)
		}
		e.Type, e.LinkTarget, e.Offset, e.Size = TypeHardLink, t.Path, t.Offset, t.Size
		c.links[t.Path]++
	case tar.TypeChar:
		e.Type = TypeCharDevice
	case tar.TypeBlock:
		e.Type = TypeBlockDevice
	case tar.TypeFifo:
		e.Type = TypeFIFO
	default:
		return fmt.Errorf("%w: %s has tar type %q", ErrInvalidLayer, p, hdr.Typeflag)
	}
	if e.Type != TypeCharDevice && e.Type != TypeBlockDevice {
		e.DevMajor, e.DevMinor = 0, 0
	}
	e.Mode = fileMode(e.Type, uint32(hdr.Mode&07777)) //nolint:gosec // Masked to 12 bits.
	if p == "." && e.Type != TypeDirectory {
		return fmt.Errorf("%w: the layer root is a %s", ErrInvalidLayer, e.Type)
	}
	return c.put(e)
}

// put records e, replacing an earlier entry of the same path. A whiteout
// hides only the lower layers: it never replaces an entry of this layer, and
// a directory that replaces a whiteout is opaque.
func (c *converter) put(e Entry) error {
	if parent := path.Dir(e.Path); e.Path != "." && parent != "." {
		if _, err := c.dir(parent, false); err != nil {
			return err
		}
	}
	i, ok := c.byPath[e.Path]
	if !ok {
		c.byPath[e.Path] = len(c.entries)
		c.entries = append(c.entries, e)
		return nil
	}
	old := c.entries[i]
	if e.Whiteout {
		if old.Type == TypeDirectory {
			c.entries[i].Opaque = true
		}
		return nil
	}
	if old.Whiteout {
		e.Opaque = e.Type == TypeDirectory
		c.entries[i] = e
		return nil
	}
	if old.Type == TypeDirectory && e.Type == TypeDirectory {
		e.Opaque = e.Opaque || old.Opaque
		c.entries[i] = e
		return nil
	}
	if err := c.drop(old); err != nil {
		return err
	}
	if old.Type == TypeDirectory {
		prefix := e.Path + "/"
		for j, below := range c.entries {
			if strings.HasPrefix(below.Path, prefix) {
				if err := c.drop(below); err != nil {
					return err
				}
				delete(c.byPath, below.Path)
				c.entries[j].Path = ""
			}
		}
	}
	c.entries[i] = e
	return nil
}

// drop accounts for an entry that is replaced or removed. Hard links keep
// naming their target, so a target cannot go.
func (c *converter) drop(e Entry) error {
	if c.links[e.Path] > 0 {
		return fmt.Errorf("%w: %s is replaced or removed while hard links name it", ErrInvalidLayer, e.Path)
	}
	if e.Type == TypeHardLink {
		c.links[e.LinkTarget]--
	}
	return nil
}

// dir returns the index of directory d, adding it and its missing parents.
// The root is added only when root is set. A directory added over a
// whiteout of this layer is opaque.
func (c *converter) dir(d string, root bool) (int, error) {
	i, ok := c.byPath[d]
	if ok && c.entries[i].Whiteout {
		c.entries[i] = Entry{
			Path: d, Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: time.Unix(0, 0).UTC(), Opaque: true,
		}
		return i, nil
	}
	if ok {
		if c.entries[i].Type != TypeDirectory {
			return 0, fmt.Errorf("%w: %s is a %s with entries below it", ErrInvalidLayer, d, c.entries[i].Type)
		}
		return i, nil
	}
	if parent := path.Dir(d); d != "." && (parent != "." || root) {
		if _, err := c.dir(parent, root); err != nil {
			return 0, err
		}
	}
	c.byPath[d] = len(c.entries)
	c.entries = append(c.entries, Entry{
		Path: d, Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: time.Unix(0, 0).UTC(),
	})
	return len(c.entries) - 1, nil
}

// write appends n bytes of r to the data stream and returns their offset. A
// file of at most one frame starts a new frame when it would span two, so
// one frame read serves it.
func (c *converter) write(r io.Reader, n int64) (int64, error) {
	if n <= FrameSize && len(c.frame) > 0 && int64(len(c.frame))+n > FrameSize {
		pad := FrameSize - len(c.frame)
		c.frame = c.frame[:FrameSize]
		clear(c.frame[FrameSize-pad:])
		c.streamSize += int64(pad)
		if err := c.flush(); err != nil {
			return 0, err
		}
	}
	off := c.streamSize
	for n > 0 {
		start := len(c.frame)
		chunk := min(n, int64(FrameSize-start))
		c.frame = c.frame[:start+int(chunk)]
		if _, err := io.ReadFull(r, c.frame[start:]); err != nil {
			return 0, tarError(err)
		}
		c.streamSize += chunk
		n -= chunk
		if len(c.frame) == FrameSize {
			if err := c.flush(); err != nil {
				return 0, err
			}
		}
	}
	return off, nil
}

// flush compresses the current frame as one zstd frame and writes it out.
func (c *converter) flush() error {
	if len(c.frame) == 0 {
		return nil
	}
	if err := c.ctx.Err(); err != nil {
		return fmt.Errorf("convert layer: %w", err)
	}
	c.packed = c.enc.EncodeAll(c.frame, c.packed[:0])
	if _, err := c.out.Write(c.packed); err != nil {
		return fmt.Errorf("write data object: %w", err)
	}
	size := int64(len(c.packed))
	c.frames = append(c.frames, Frame{Offset: c.dataSize, Size: size})
	c.dataSize += size
	c.frame = c.frame[:0]
	return nil
}

// cleanPath resolves a tar name inside the layer root, as extraction does:
// leading slashes and ".." cannot leave it.
func cleanPath(name string) string {
	p := strings.TrimPrefix(path.Clean("/"+name), "/")
	if p == "" {
		return "."
	}
	return p
}

func headerEntry(p string, hdr *tar.Header) (Entry, error) {
	for _, id := range []int64{int64(hdr.Uid), int64(hdr.Gid), hdr.Devmajor, hdr.Devminor} {
		if id < 0 || id > math.MaxUint32 {
			return Entry{}, fmt.Errorf("%w: %s has owner or device number %d", ErrInvalidLayer, p, id)
		}
	}
	e := Entry{
		Path:     p,
		UID:      uint32(hdr.Uid),      //nolint:gosec // Checked above.
		GID:      uint32(hdr.Gid),      //nolint:gosec // Checked above.
		DevMajor: uint32(hdr.Devmajor), //nolint:gosec // Checked above.
		DevMinor: uint32(hdr.Devminor), //nolint:gosec // Checked above.
		ModTime:  hdr.ModTime.UTC(),
	}
	for k, v := range hdr.PAXRecords {
		if name, ok := strings.CutPrefix(k, xattrPrefix); ok {
			if e.Xattrs == nil {
				e.Xattrs = map[string][]byte{}
			}
			e.Xattrs[name] = []byte(v)
		}
	}
	return e, nil
}

// fileMode builds a Go file mode from an entry type and the POSIX
// permission, setuid, setgid and sticky bits.
func fileMode(t EntryType, posix uint32) fs.FileMode {
	m := fs.FileMode(posix & 0o777)
	if posix&0o4000 != 0 {
		m |= fs.ModeSetuid
	}
	if posix&0o2000 != 0 {
		m |= fs.ModeSetgid
	}
	if posix&0o1000 != 0 {
		m |= fs.ModeSticky
	}
	switch t {
	case TypeDirectory:
		m |= fs.ModeDir
	case TypeSymlink:
		m |= fs.ModeSymlink
	case TypeCharDevice:
		m |= fs.ModeDevice | fs.ModeCharDevice
	case TypeBlockDevice:
		m |= fs.ModeDevice
	case TypeFIFO:
		m |= fs.ModeNamedPipe
	case TypeRegular, TypeHardLink:
	}
	return m
}

// posixMode is fileMode's inverse for the bits the index stores.
func posixMode(m fs.FileMode) uint32 {
	posix := uint32(m.Perm())
	if m&fs.ModeSetuid != 0 {
		posix |= 0o4000
	}
	if m&fs.ModeSetgid != 0 {
		posix |= 0o2000
	}
	if m&fs.ModeSticky != 0 {
		posix |= 0o1000
	}
	return posix
}

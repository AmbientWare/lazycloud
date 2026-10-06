// Package imagefs owns the stored layer format of lazy images: converting an
// OCI layer tar into a framed zstd data object and an index of its file tree,
// encoding that index, and reading frames back by HTTP range. The schema is
// contracts/imagefs/v1/index.proto. It has no database access.
package imagefs

import (
	"errors"
	"io/fs"
	"time"
)

// Digest is a layer's uncompressed digest (diff_id), "sha256:<hex>".
type Digest string

// FrameSize is the uncompressed size of every data frame but the last.
const FrameSize = 4 << 20

// Index is one layer's file tree and the frame table of its data object.
type Index struct {
	Layer      Digest
	StreamSize int64   // bytes of the uncompressed data stream
	DataSize   int64   // bytes of the stored, compressed data object
	Frames     []Frame // frame i holds uncompressed bytes [i*FrameSize, (i+1)*FrameSize)
	Entries    []Entry // in tar order; directories before their children
}

// Frame is one independently compressed zstd frame of the data object.
type Frame struct {
	Offset int64 // in the data object
	Size   int64 // compressed bytes
}

// EntryType is the kind of file an entry describes.
type EntryType string

const (
	TypeRegular     EntryType = "regular"
	TypeDirectory   EntryType = "directory"
	TypeSymlink     EntryType = "symlink"
	TypeHardLink    EntryType = "hard_link"
	TypeCharDevice  EntryType = "char_device"
	TypeBlockDevice EntryType = "block_device"
	TypeFIFO        EntryType = "fifo"
)

// Entry is one path of the layer.
//
// A hard link's LinkTarget names the earlier entry that holds the file, never
// another hard link, and its Offset and Size repeat that entry's bytes. A
// whiteout is a character device 0:0 at the removed path, as overlayfs reads
// it; Opaque marks a directory that hides the lower layers' contents.
type Entry struct {
	Path               string // clean, relative, no leading "./"; the root is "."
	Type               EntryType
	Mode               fs.FileMode
	UID, GID           uint32
	ModTime            time.Time
	LinkTarget         string // symlinks and hard links
	DevMajor, DevMinor uint32
	Xattrs             map[string][]byte
	Whiteout, Opaque   bool  // OCI whiteout and opaque directory markers
	Offset, Size       int64 // a regular file's bytes in the uncompressed data stream
}

var (
	// ErrInvalidLayer marks a layer tar Convert refuses: a path outside the
	// root, a hard link to a missing or unlinkable entry, or an unknown entry
	// type.
	ErrInvalidLayer = errors.New("invalid layer")
	// ErrUnsupportedVersion marks a stored index written in a format version
	// this reader does not know.
	ErrUnsupportedVersion = errors.New("unsupported index format version")
	// ErrInvalidIndex marks a stored index or data object that breaks the
	// format's invariants.
	ErrInvalidIndex = errors.New("invalid index")
)

// FrameSpan is the frames holding bytes [off, off+n) of e's contents. The
// range is clipped to the file; an empty result has first > last.
func (ix Index) FrameSpan(e Entry, off, n int64) (first, last int) {
	off = max(off, 0)
	if n <= 0 || off >= e.Size {
		return 0, -1
	}
	end := e.Size
	if n < e.Size-off {
		end = off + n
	}
	return int((e.Offset + off) / FrameSize), int((e.Offset + end - 1) / FrameSize)
}

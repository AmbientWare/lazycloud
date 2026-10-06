// Package imagefs owns the stored layer format of lazy images: converting an
// OCI layer tar into a framed zstd data object and an index of its file tree,
// encoding that index, uploading the pair, and reading frames back by HTTP
// range. The schema is contracts/imagefs/v1/index.proto. It has no database
// access.
package imagefs

import (
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"strings"
	"time"
)

// Digest is a layer's uncompressed digest (diff_id), "sha256:<hex>".
type Digest string

// Check reports whether d is "sha256:" and 64 lowercase hex digits.
func (d Digest) Check() error {
	h, ok := strings.CutPrefix(string(d), "sha256:")
	if _, err := hex.DecodeString(h); !ok || len(h) != 64 || strings.ToLower(h) != h || err != nil {
		return fmt.Errorf("layer digest %q is not sha256:<64 lowercase hex>", string(d))
	}
	return nil
}

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

// FrameLen is the uncompressed length of frame i.
func (ix Index) FrameLen(i int) int {
	return int(min(FrameSize, ix.StreamSize-int64(i)*FrameSize))
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
	// ErrInvalidLayer marks a layer conversion refuses: a malformed tar, a
	// path outside the root, a hard link to a missing or unlinkable entry,
	// an unknown entry type, or content that is not the named diff_id.
	ErrInvalidLayer = errors.New("invalid layer")
	// ErrUnsupportedVersion marks a stored index written in a format version
	// this reader does not know.
	ErrUnsupportedVersion = errors.New("unsupported index format version")
	// ErrInvalidIndex marks a stored index or data object that breaks the
	// format's invariants.
	ErrInvalidIndex = errors.New("invalid index")
)

// ceilDiv is n/d rounded up, for n >= 0 and d > 0.
func ceilDiv(n, d int64) int64 { return (n + d - 1) / d }

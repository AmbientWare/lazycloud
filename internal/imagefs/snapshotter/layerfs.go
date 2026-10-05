package snapshotter

import (
	"context"
	"io/fs"
	"log/slog"
	"maps"
	"path"
	"slices"
	"syscall"
	"time"

	gofs "github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

// opaqueXattr marks an opaque directory to the kernel's overlayfs, which
// reads it from lower layers mounted as trusted.
const opaqueXattr = "trusted.overlay.opaque"

// layer is one converted layer a mount reads.
type layer struct {
	digest imagefs.Digest
	index  imagefs.Index
	// data reads the data object through the layer's current grant.
	data   imagefs.RangeReader
	frames *frameCache
	log    *slog.Logger
	failed func()
}

// read fills dest with e's bytes from off and returns how many it read. A
// read the store cannot serve fails whole; it never returns partial or
// zeroed bytes.
func (l *layer) read(ctx context.Context, e *imagefs.Entry, dest []byte, off int64) (int, error) {
	if off >= e.Size {
		return 0, nil
	}
	n := min(int64(len(dest)), e.Size-off)
	start := e.Offset + off
	for pos := start; pos < start+n; {
		frame := pos / imagefs.FrameSize
		frameStart := frame * imagefs.FrameSize
		end := min(start+n, frameStart+imagefs.FrameSize)
		if err := l.frames.read(ctx, l, int(frame), dest[pos-start:end-start], pos-frameStart); err != nil {
			return 0, err
		}
		pos = end
	}
	next := (start+n-1)/imagefs.FrameSize + 1
	last := (e.Offset + e.Size - 1) / imagefs.FrameSize
	for f := next; f <= min(last, next+readAhead-1); f++ {
		l.frames.prefetch(l, int(f))
	}
	return int(n), nil
}

// node is one inode of a mounted layer. Hard links share one node.
type node struct {
	gofs.Inode
	layer *layer
	entry *imagefs.Entry
	ino   uint64
	nlink uint32
}

var (
	_ gofs.NodeGetattrer   = (*node)(nil)
	_ gofs.NodeGetxattrer  = (*node)(nil)
	_ gofs.NodeListxattrer = (*node)(nil)
	_ gofs.NodeReadlinker  = (*node)(nil)
	_ gofs.NodeOpener      = (*node)(nil)
	_ gofs.NodeReader      = (*node)(nil)
)

// rootEntry stands in for a layer whose tar has no "." member.
func rootEntry() *imagefs.Entry {
	return &imagefs.Entry{Path: ".", Type: imagefs.TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: time.Unix(0, 0).UTC()}
}

// newRoot returns the root of l's file tree; build fills it in once it is
// mounted.
func newRoot(l *layer) *node {
	root := &node{layer: l, entry: rootEntry(), ino: 1, nlink: 2}
	for i := range l.index.Entries {
		if l.index.Entries[i].Path == "." {
			root.entry = &l.index.Entries[i]
		}
	}
	return root
}

// build adds the whole tree below the root. The index lists every
// directory before its children and every hard link after its file.
func (n *node) build(ctx context.Context) {
	byPath := map[string]*gofs.Inode{".": &n.Inode}
	for i := range n.layer.index.Entries {
		e := &n.layer.index.Entries[i]
		if e.Path == "." {
			continue
		}
		parent := byPath[path.Dir(e.Path)]
		if parent == nil {
			continue
		}
		name := path.Base(e.Path)
		if e.Type == imagefs.TypeHardLink {
			target := byPath[e.LinkTarget]
			if target == nil {
				continue
			}
			target.Operations().(*node).nlink++ //nolint:forcetypeassert // every inode here is a node
			parent.AddChild(name, target, true)
			continue
		}
		child := &node{layer: n.layer, entry: e, ino: uint64(i) + 2, nlink: 1}
		if e.Type == imagefs.TypeDirectory {
			child.nlink = 2
			parent.Operations().(*node).nlink++ //nolint:forcetypeassert // every inode here is a node
		}
		inode := parent.NewPersistentInode(ctx, child, gofs.StableAttr{Mode: fileType(e.Type), Ino: child.ino})
		parent.AddChild(name, inode, true)
		byPath[e.Path] = inode
	}
}

func (n *node) Getattr(_ context.Context, _ gofs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	e := n.entry
	a := &out.Attr
	a.Ino = n.ino
	a.Mode = fileType(e.Type) | permBits(e.Mode)
	a.Nlink = n.nlink
	a.Uid, a.Gid = e.UID, e.GID
	a.Rdev = uint32(unix.Mkdev(e.DevMajor, e.DevMinor)) //nolint:gosec // the kernel's 32-bit device encoding
	switch e.Type {
	case imagefs.TypeRegular, imagefs.TypeHardLink:
		a.Size = uint64(e.Size) //nolint:gosec // sizes are validated non-negative
	case imagefs.TypeSymlink:
		a.Size = uint64(len(e.LinkTarget))
	case imagefs.TypeDirectory:
		a.Size = 4096
	case imagefs.TypeCharDevice, imagefs.TypeBlockDevice, imagefs.TypeFIFO:
	}
	a.Blocks = (a.Size + 511) / 512
	a.Blksize = 4096
	t := e.ModTime
	a.SetTimes(&t, &t, &t)
	return 0
}

func (n *node) xattrs() map[string][]byte {
	if !n.entry.Opaque {
		return n.entry.Xattrs
	}
	all := maps.Clone(n.entry.Xattrs)
	if all == nil {
		all = make(map[string][]byte, 1)
	}
	all[opaqueXattr] = []byte("y")
	return all
}

func (n *node) Getxattr(_ context.Context, attr string, dest []byte) (uint32, syscall.Errno) {
	v, ok := n.xattrs()[attr]
	if !ok {
		return 0, syscall.ENODATA
	}
	if len(dest) == 0 {
		return uint32(len(v)), 0 //nolint:gosec // xattr values are small
	}
	if len(dest) < len(v) {
		return uint32(len(v)), syscall.ERANGE //nolint:gosec // xattr values are small
	}
	return uint32(copy(dest, v)), 0 //nolint:gosec // xattr values are small
}

func (n *node) Listxattr(_ context.Context, dest []byte) (uint32, syscall.Errno) {
	var list []byte
	for _, name := range slices.Sorted(maps.Keys(n.xattrs())) {
		list = append(append(list, name...), 0)
	}
	if len(dest) == 0 {
		return uint32(len(list)), 0 //nolint:gosec // xattr names are small
	}
	if len(dest) < len(list) {
		return uint32(len(list)), syscall.ERANGE //nolint:gosec // xattr names are small
	}
	return uint32(copy(dest, list)), 0 //nolint:gosec // xattr names are small
}

func (n *node) Readlink(context.Context) ([]byte, syscall.Errno) {
	if n.entry.Type != imagefs.TypeSymlink {
		return nil, syscall.EINVAL
	}
	return []byte(n.entry.LinkTarget), 0
}

// Open lets the kernel keep a file's pages: layer contents never change.
func (n *node) Open(_ context.Context, flags uint32) (gofs.FileHandle, uint32, syscall.Errno) {
	if flags&(syscall.O_WRONLY|syscall.O_RDWR|syscall.O_TRUNC|syscall.O_APPEND) != 0 {
		return nil, 0, syscall.EROFS
	}
	return nil, fuse.FOPEN_KEEP_CACHE, 0
}

// Read fails with EIO when the store cannot serve the bytes, logged and
// counted.
func (n *node) Read(ctx context.Context, _ gofs.FileHandle, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	got, err := n.layer.read(ctx, n.entry, dest, off)
	if err != nil {
		n.layer.failed()
		n.layer.log.ErrorContext(ctx, "layer read failed", "layer", n.layer.digest, "path", n.entry.Path, "offset", off, "error", err)
		return nil, syscall.EIO
	}
	return fuse.ReadResultData(dest[:got]), 0
}

func fileType(t imagefs.EntryType) uint32 {
	switch t {
	case imagefs.TypeRegular, imagefs.TypeHardLink:
		return syscall.S_IFREG
	case imagefs.TypeDirectory:
		return syscall.S_IFDIR
	case imagefs.TypeSymlink:
		return syscall.S_IFLNK
	case imagefs.TypeCharDevice:
		return syscall.S_IFCHR
	case imagefs.TypeBlockDevice:
		return syscall.S_IFBLK
	case imagefs.TypeFIFO:
		return syscall.S_IFIFO
	}
	return syscall.S_IFREG
}

func permBits(m fs.FileMode) uint32 {
	bits := uint32(m.Perm())
	if m&fs.ModeSetuid != 0 {
		bits |= syscall.S_ISUID
	}
	if m&fs.ModeSetgid != 0 {
		bits |= syscall.S_ISGID
	}
	if m&fs.ModeSticky != 0 {
		bits |= syscall.S_ISVTX
	}
	return bits
}

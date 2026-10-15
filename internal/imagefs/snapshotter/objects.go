package snapshotter

import (
	"container/list"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"
)

// A volume object version is stored as one sparse file in the cache
// directory, each frame written at its offset as it arrives, so a stored
// frame is read in place and a complete version is one readable file.
// Once complete, the file is hard-linked into the link tree at the
// version's link path, where workloads' preload library opens it in place
// of the mount's file; the link goes when the object changes or the version
// is evicted, and a reader that holds it open keeps the unlinked file.

// objectSource is a frameSource stored as one file: a volume object
// version.
type objectSource interface {
	frameSource
	object() objectInfo
}

// objectInfo is what the cache keeps of a version besides its frames: its
// size, the store's modification time, which the file carries once
// complete so the preload library matches it to the mount's, and where in
// the link tree the complete file goes, empty for none.
type objectInfo struct {
	size     int64
	modified time.Time
	link     string
}

// storedObject is a version with some or all frames stored in its file.
// Guarded by frameCache.mu.
type storedObject struct {
	key     string
	path    string
	info    objectInfo
	frames  []bool
	present int
	// stored is the bytes written, counted as used; used is when a frame
	// was last read or stored, which orders eviction against frames.
	stored int64
	used   time.Time
	// linked is set once the complete file is in the link tree.
	linked bool
	elem   *list.Element
}

func (o *storedObject) complete() bool { return o.present == len(o.frames) }

// victims is what an eviction unlisted, for remove.
type victims struct {
	frames  []*cachedFrame
	objects []*storedObject
}

// storedFrame reads frame of src from its object's file into p from off,
// and reports whether the file held it.
func (c *frameCache) storedFrame(src objectSource, frame int, p []byte, off int64) bool {
	c.mu.Lock()
	o := c.objects[src.key(frame).object]
	if o == nil || frame >= len(o.frames) || !o.frames[frame] {
		c.mu.Unlock()
		return false
	}
	o.used = time.Now()
	c.objectList.MoveToFront(o.elem)
	path := o.path
	c.mu.Unlock()
	return readAt(path, p, int64(frame)*volumeFrame+off) == nil
}

// storeObjectFrame writes frame k of src at its offset in the version's
// file, making the file on its first frame, and links the file into the
// link tree once every frame is in it.
func (c *frameCache) storeObjectFrame(src objectSource, k frameKey, data []byte) error {
	info := src.object()
	c.mu.Lock()
	evicted := c.evictLocked(victims{}, int64(len(data)))
	o := c.objects[k.object]
	if o == nil {
		file, err := os.CreateTemp(c.dir, "object-")
		if err != nil {
			c.mu.Unlock()
			c.remove(evicted)
			return fmt.Errorf("store object: %w", err)
		}
		err = file.Truncate(info.size)
		if cerr := file.Close(); err == nil {
			err = cerr
		}
		if err != nil {
			c.mu.Unlock()
			c.remove(evicted)
			_ = os.Remove(file.Name())
			return fmt.Errorf("store object: %w", err)
		}
		o = &storedObject{key: k.object, path: file.Name(), info: info, frames: make([]bool, (info.size+volumeFrame-1)/volumeFrame), used: time.Now()}
		o.elem = c.objectList.PushFront(o)
		c.objects[k.object] = o
	}
	path := o.path
	c.mu.Unlock()
	c.remove(evicted)
	if k.frame >= len(o.frames) {
		return fmt.Errorf("store object: frame %d of %s is past its %d frames", k.frame, k.object, len(o.frames))
	}
	file, err := os.OpenFile(path, os.O_WRONLY, 0) //nolint:gosec // a file of the cache
	if err != nil {
		return fmt.Errorf("store object frame: %w", err)
	}
	_, err = file.WriteAt(data, int64(k.frame)*volumeFrame)
	if cerr := file.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		return fmt.Errorf("store object frame: %w", err)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.objects[k.object] != o || o.frames[k.frame] {
		return nil
	}
	o.frames[k.frame] = true
	o.present++
	o.stored += int64(len(data))
	o.used = time.Now()
	c.used += int64(len(data))
	c.free -= int64(len(data))
	if o.complete() && o.info.link != "" {
		if err := c.linkObject(o); err != nil {
			c.log.Warn("linking a complete volume object failed", "object", o.key, "error", err)
		}
	}
	return nil
}

// linkObject puts o's complete file at its link path, in place of any
// file there, with the store's modification time; c.mu is held.
func (c *frameCache) linkObject(o *storedObject) error {
	if err := os.Chtimes(o.path, o.info.modified, o.info.modified); err != nil {
		return fmt.Errorf("set the file's time: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(o.info.link), 0o755); err != nil { //nolint:gosec // Workloads read the tree.
		return fmt.Errorf("make the link's directory: %w", err)
	}
	tmp := o.info.link + ".linking"
	_ = os.Remove(tmp)
	if err := os.Link(o.path, tmp); err != nil {
		return fmt.Errorf("link the file: %w", err)
	}
	if err := os.Rename(tmp, o.info.link); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("place the link: %w", err)
	}
	if other := c.links[o.info.link]; other != nil && other != o {
		other.linked = false
	}
	c.links[o.info.link] = o
	o.linked = true
	return nil
}

// dropLink removes the link tree's file at link unless it is version's,
// so a workload never opens a version the mount no longer names.
func (c *frameCache) dropLink(link, version string) {
	if link == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	o := c.links[link]
	if o != nil && o.key == version {
		return
	}
	if err := os.Remove(link); err != nil && !errors.Is(err, fs.ErrNotExist) {
		c.log.Warn("removing a volume object's link failed", "link", link, "error", err)
		return
	}
	delete(c.links, link)
	if o != nil {
		o.linked = false
	}
}

// unlistObjectLocked unlists o and counts its bytes free of the bound.
func (c *frameCache) unlistObjectLocked(o *storedObject) {
	c.objectList.Remove(o.elem)
	delete(c.objects, o.key)
	if o.linked && c.links[o.info.link] == o {
		delete(c.links, o.info.link)
	}
	c.used -= o.stored
}

// removeObject deletes o's file and its link. An open reader keeps the
// file until it closes, so the space counts free once it does.
func (c *frameCache) removeObject(o *storedObject) int64 {
	if o.linked {
		if err := os.Remove(o.info.link); err != nil && !errors.Is(err, fs.ErrNotExist) {
			c.log.Warn("removing a volume object's link failed", "link", o.info.link, "error", err)
		}
	}
	if err := os.Remove(o.path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		c.log.Warn("evicting a volume object failed", "object", o.key, "error", err)
	}
	return o.stored
}

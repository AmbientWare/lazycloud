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
	// linked is set while the version holds its link path.
	linked bool
	elem   *list.Element
}

func (o *storedObject) complete() bool { return o.present == len(o.frames) }

// victims is what an eviction unlisted, for remove.
type victims struct {
	frames  []*cachedFrame
	objects []*storedObject
}

// objectFrame returns the file holding frame k and the frame's length when
// the frame is stored in its object's file, and marks the object used.
func (c *frameCache) objectFrame(k frameKey) (path string, n int, ok bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	o := c.objects[k.object]
	if o == nil || k.frame >= len(o.frames) || !o.frames[k.frame] {
		return "", 0, false
	}
	o.used = time.Now()
	c.objectList.MoveToFront(o.elem)
	return o.path, int(min(volumeFrame, o.info.size-int64(k.frame)*volumeFrame)), true
}

// storeObjectFrame writes frame k of src at its offset in the version's
// file, making the file on its first frame, and links the file into the
// link tree once every frame is in it. No file operation runs under the
// cache's lock: a slow disk must never hold a read.
func (c *frameCache) storeObjectFrame(src objectSource, k frameKey, data []byte) error {
	info := src.object()
	c.mu.Lock()
	o := c.objects[k.object]
	if o != nil {
		o.used = time.Now()
		c.objectList.MoveToFront(o.elem)
	}
	evicted := c.evictLocked(victims{}, int64(len(data)), o)
	c.mu.Unlock()
	c.remove(evicted)
	if o == nil {
		file, err := os.CreateTemp(c.dir, "object-")
		if err != nil {
			return fmt.Errorf("store object: %w", err)
		}
		// Workloads of any user open the file once it is linked.
		if err = file.Chmod(0o644); err == nil {
			err = file.Truncate(info.size)
		}
		if cerr := file.Close(); err == nil {
			err = cerr
		}
		if err != nil {
			_ = os.Remove(file.Name())
			return fmt.Errorf("store object: %w", err)
		}
		o = &storedObject{key: k.object, path: file.Name(), info: info, frames: make([]bool, (info.size+volumeFrame-1)/volumeFrame), used: time.Now()}
		c.mu.Lock()
		if other := c.objects[k.object]; other != nil {
			c.mu.Unlock()
			_ = os.Remove(file.Name())
			o = other
		} else {
			o.elem = c.objectList.PushFront(o)
			c.objects[k.object] = o
			c.mu.Unlock()
		}
	}
	if k.frame >= len(o.frames) {
		return fmt.Errorf("store object: frame %d of %s is past its %d frames", k.frame, k.object, len(o.frames))
	}
	file, err := os.OpenFile(o.path, os.O_WRONLY, 0) //nolint:gosec // a file of the cache
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
	if c.objects[k.object] != o || o.frames[k.frame] {
		c.mu.Unlock()
		return nil
	}
	o.frames[k.frame] = true
	o.present++
	o.stored += int64(len(data))
	o.used = time.Now()
	c.used += int64(len(data))
	c.free -= int64(len(data))
	complete := o.complete() && o.info.link != "" && !o.linked
	c.mu.Unlock()
	if complete {
		if err := c.linkObject(o); err != nil {
			c.log.Warn("linking a complete volume object failed", "object", o.key, "error", err)
		}
	}
	return nil
}

// linkObject puts o's complete file at its link path, in place of any
// file there, with the store's modification time. o takes the path under
// mu before any file moves. The move, like every removal in the tree, runs
// under linkMu and only while o still holds the path, so a version evicted
// as another is linked never removes the other's link.
func (c *frameCache) linkObject(o *storedObject) error {
	link := o.info.link
	c.mu.Lock()
	if c.objects[o.key] != o {
		c.mu.Unlock()
		return nil
	}
	if other := c.links[link]; other != nil && other != o {
		other.linked = false
	}
	c.links[link] = o
	o.linked = true
	c.mu.Unlock()
	c.linkMu.Lock()
	defer c.linkMu.Unlock()
	if !c.holdsLink(o) {
		return nil
	}
	if err := os.Chtimes(o.path, o.info.modified, o.info.modified); err != nil {
		return fmt.Errorf("set the file's time: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(link), 0o755); err != nil { //nolint:gosec // Workloads read the tree.
		return fmt.Errorf("make the link's directory: %w", err)
	}
	tmp := link + ".linking"
	_ = os.Remove(tmp)
	if err := os.Link(o.path, tmp); err != nil {
		return fmt.Errorf("link the file: %w", err)
	}
	if err := os.Rename(tmp, link); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("place the link: %w", err)
	}
	return nil
}

// holdsLink reports whether o holds its link path.
func (c *frameCache) holdsLink(o *storedObject) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.links[o.info.link] == o
}

// unlink removes the link tree's file at o's link path while o holds it.
func (c *frameCache) unlink(o *storedObject) {
	c.linkMu.Lock()
	defer c.linkMu.Unlock()
	c.mu.Lock()
	held := c.links[o.info.link] == o
	if held {
		delete(c.links, o.info.link)
		o.linked = false
	}
	c.mu.Unlock()
	if !held {
		return
	}
	if err := os.Remove(o.info.link); err != nil && !errors.Is(err, fs.ErrNotExist) {
		c.log.Warn("removing a volume object's link failed", "link", o.info.link, "error", err)
	}
}

// dropLink removes the link tree's file at link unless it is version's,
// so a workload never opens a version the mount no longer names. The tree
// starts empty with the cache, so links names every file in it.
func (c *frameCache) dropLink(link, version string) {
	c.mu.Lock()
	o := c.links[link]
	c.mu.Unlock()
	if o != nil && o.key != version {
		c.unlink(o)
	}
}

// unlistObjectLocked unlists o and counts its bytes free of the bound.
func (c *frameCache) unlistObjectLocked(o *storedObject) {
	c.objectList.Remove(o.elem)
	delete(c.objects, o.key)
	c.used -= o.stored
}

// removeObject deletes o's file and its link. An open reader keeps the
// file until it closes, so the space counts free once it does.
func (c *frameCache) removeObject(o *storedObject) int64 {
	if o.info.link != "" {
		c.unlink(o)
	}
	if err := os.Remove(o.path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		c.log.Warn("evicting a volume object failed", "object", o.key, "error", err)
	}
	return o.stored
}

// clearLinks empties the link tree of files, keeping its directories: a
// container keeps the directory of its volume bound while the snapshotter
// restarts under it.
func clearLinks(dir string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil { //nolint:gosec // Workloads read the tree.
		return fmt.Errorf("create volume link tree: %w", err)
	}
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		return os.Remove(path) //nolint:gosec // the snapshotter alone writes the tree
	})
	if err != nil {
		return fmt.Errorf("clear volume link tree: %w", err)
	}
	return nil
}

// touchObject marks the version's object used, if stored.
func (c *frameCache) touchObject(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if o := c.objects[key]; o != nil {
		o.used = time.Now()
		c.objectList.MoveToFront(o.elem)
	}
}

// canStore reports whether n more bytes fit the volume's free space above
// its reserve: a fill of what cannot be stored would only read the store.
func (c *frameCache) canStore(n int64) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.free-n >= c.reserve
}

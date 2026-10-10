package agent

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"golang.org/x/sys/unix"
)

const (
	// buildCacheBytes bounds the build caches of one host together.
	buildCacheBytes = 20 << 30
	// buildStateKeepMB is what BuildKit's prune leaves of one state when its
	// build ends, in the megabytes buildctl takes.
	buildStateKeepMB = "10240"
)

// buildCaches owns the build caches under dir, one directory per workspace
// that built on this host. A workspace's directory holds a BuildKit state
// for each of its builds that ran at once, <dir>/<workspace>/<n>, so two
// builds never share a BuildKit store. A builder mounts only the state it
// holds, so a build reads no other workspace's layers or cache mounts. A
// build that names no workspace gets an empty state of its own under
// <dir>/unscoped, removed once its build ends. When a build ends, whole
// workspaces that run no build are removed, least recently used first,
// until the caches fit in limit.
type buildCaches struct {
	dir   string
	limit int64
	log   *slog.Logger
	// passes holds a token while an eviction pass runs.
	passes chan struct{}

	mu sync.Mutex
	// held are the states builds use now.
	held map[string]bool
	// evicting has a channel for each workspace being removed, closed once
	// it is gone.
	evicting map[string]chan struct{}
	// sizes are the bytes of each state, measured by the first eviction pass
	// after the agent starts or the state's build ends.
	sizes map[string]int64
}

func newBuildCaches(dir string, limit int64, log *slog.Logger) *buildCaches {
	return &buildCaches{
		dir: dir, limit: limit, log: log, passes: make(chan struct{}, 1),
		held: map[string]bool{}, evicting: map[string]chan struct{}{}, sizes: map[string]int64{},
	}
}

// unscopedDir holds the states of builds that name no workspace.
const unscopedDir = "unscoped"

// acquire holds a state of workspace's cache for one build and returns its
// directory, which the builder's user can write. With no workspace it is a
// new, empty state.
func (b *buildCaches) acquire(ctx context.Context, workspace string) (string, error) {
	if workspace == "" {
		return b.acquireUnscoped()
	}
	id, err := uuid.Parse(workspace)
	if err != nil {
		return "", fmt.Errorf("the build names no workspace: %w", err)
	}
	dir := filepath.Join(b.dir, id.String())
	b.mu.Lock()
	for gone := b.evicting[dir]; gone != nil; gone = b.evicting[dir] {
		b.mu.Unlock()
		select {
		case <-gone:
		case <-ctx.Done():
			return "", fmt.Errorf("wait for the build cache: %w", ctx.Err())
		}
		b.mu.Lock()
	}
	n := 0
	for b.held[filepath.Join(dir, strconv.Itoa(n))] {
		n++
	}
	state := filepath.Join(dir, strconv.Itoa(n))
	b.held[state] = true
	b.mu.Unlock()
	if err := openState(dir, state); err != nil {
		b.mu.Lock()
		delete(b.held, state)
		b.mu.Unlock()
		return "", err
	}
	return state, nil
}

func (b *buildCaches) acquireUnscoped() (string, error) {
	dir := filepath.Join(b.dir, unscopedDir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("create the build state: %w", err)
	}
	state, err := os.MkdirTemp(dir, "state")
	if err != nil {
		return "", fmt.Errorf("create the build state: %w", err)
	}
	if err := os.Chmod(state, 0o777); err != nil { //nolint:gosec // The builder runs as its own unprivileged user.
		return "", fmt.Errorf("open the build state: %w", err)
	}
	b.mu.Lock()
	b.held[state] = true
	b.mu.Unlock()
	return state, nil
}

// openState creates state and marks its workspace used now.
func openState(workspace, state string) error {
	if err := os.MkdirAll(workspace, 0o700); err != nil {
		return fmt.Errorf("create the build cache: %w", err)
	}
	if err := os.Mkdir(state, 0o700); err != nil && !errors.Is(err, fs.ErrExist) {
		return fmt.Errorf("create the build cache: %w", err)
	}
	// The builder runs as its own unprivileged user; only it mounts state.
	if err := os.Chmod(state, 0o777); err != nil { //nolint:gosec // As above.
		return fmt.Errorf("open the build cache: %w", err)
	}
	now := time.Now()
	if err := os.Chtimes(workspace, now, now); err != nil {
		return fmt.Errorf("mark the build cache used: %w", err)
	}
	return nil
}

// release ends a build's hold on state, so the workspace's next build can
// take it. evict then measures it, removes it if it names no workspace, and
// brings the caches back within their limit.
func (b *buildCaches) release(ctx context.Context, state string) {
	now := time.Now()
	if err := os.Chtimes(filepath.Dir(state), now, now); err != nil {
		b.log.WarnContext(ctx, "marking the build cache used failed", "error", err)
	}
	b.mu.Lock()
	delete(b.held, state)
	delete(b.sizes, state)
	b.mu.Unlock()
}

// workspaceCache is one workspace's directory on disk.
type workspaceCache struct {
	dir    string
	used   time.Time
	bytes  int64
	states []string
}

// evict removes whole workspaces that run no build, least recently used
// first, until the caches fit in the limit.
func (b *buildCaches) evict(ctx context.Context) {
	select {
	case b.passes <- struct{}{}:
	case <-ctx.Done():
		return
	}
	defer func() { <-b.passes }()
	b.removeUnscoped(ctx)
	caches, total, err := b.measure()
	if err != nil {
		b.log.Warn("measuring the build caches failed", "error", err)
		return
	}
	slices.SortFunc(caches, func(x, y workspaceCache) int { return cmp.Or(x.used.Compare(y.used), strings.Compare(x.dir, y.dir)) })
	for _, w := range caches {
		if total <= b.limit || ctx.Err() != nil {
			return
		}
		b.mu.Lock()
		busy := false
		for state := range b.held {
			busy = busy || filepath.Dir(state) == w.dir
		}
		if busy {
			b.mu.Unlock()
			continue
		}
		gone := make(chan struct{})
		b.evicting[w.dir] = gone
		b.mu.Unlock()
		err := os.RemoveAll(w.dir)
		b.mu.Lock()
		delete(b.evicting, w.dir)
		for _, state := range w.states {
			delete(b.sizes, state)
		}
		close(gone)
		b.mu.Unlock()
		if err != nil {
			b.log.Warn("removing a build cache failed", "dir", w.dir, "error", err)
			continue
		}
		total -= w.bytes
		b.log.Info("removed a build cache", "dir", w.dir, "bytes", w.bytes, "last_used", w.used)
	}
}

// removeUnscoped removes the states of ended builds that named no
// workspace.
func (b *buildCaches) removeUnscoped(ctx context.Context) {
	dir := filepath.Join(b.dir, unscopedDir)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		state := filepath.Join(dir, entry.Name())
		b.mu.Lock()
		held := b.held[state]
		b.mu.Unlock()
		if held {
			continue
		}
		if err := os.RemoveAll(state); err != nil {
			b.log.WarnContext(ctx, "removing a build state failed", "dir", state, "error", err)
		}
	}
}

// measure lists the workspace caches with their sizes and the total. A
// state a build holds counts what it took when last measured.
func (b *buildCaches) measure() ([]workspaceCache, int64, error) {
	entries, err := os.ReadDir(b.dir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, 0, nil
	}
	if err != nil {
		return nil, 0, fmt.Errorf("list the build caches: %w", err)
	}
	var caches []workspaceCache
	var total int64
	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil || !info.IsDir() || entry.Name() == unscopedDir {
			continue
		}
		w := workspaceCache{dir: filepath.Join(b.dir, entry.Name()), used: info.ModTime()}
		states, err := os.ReadDir(w.dir)
		if err != nil {
			return nil, 0, fmt.Errorf("list a build cache: %w", err)
		}
		for _, s := range states {
			state := filepath.Join(w.dir, s.Name())
			w.states = append(w.states, state)
			b.mu.Lock()
			size, known := b.sizes[state]
			held := b.held[state]
			b.mu.Unlock()
			if !known && !held {
				if size, err = treeBytes(state); err != nil {
					return nil, 0, err
				}
				b.mu.Lock()
				b.sizes[state] = size
				b.mu.Unlock()
			}
			w.bytes += size
		}
		total += w.bytes
		caches = append(caches, w)
	}
	return caches, total, nil
}

// treeBytes is the disk space the files under dir occupy.
func treeBytes(dir string) (int64, error) {
	var total int64
	err := filepath.WalkDir(dir, func(path string, _ fs.DirEntry, err error) error {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		var st unix.Stat_t
		if err := unix.Lstat(path, &st); err != nil {
			if errors.Is(err, unix.ENOENT) {
				return nil
			}
			return fmt.Errorf("stat %s: %w", path, err)
		}
		total += st.Blocks * 512
		return nil
	})
	if err != nil {
		return 0, fmt.Errorf("measure %s: %w", dir, err)
	}
	return total, nil
}

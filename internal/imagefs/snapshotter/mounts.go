package snapshotter

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/containerd/containerd/v2/core/snapshots"
	"github.com/containerd/containerd/v2/core/snapshots/storage"
	"github.com/containerd/errdefs"
	gofs "github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

const (
	// fuseType is the mounts' filesystem type, which finds those a previous
	// process left.
	fuseType = "fuse." + layersource.Snapshotter
	// maxRead is the largest read the kernel sends.
	maxRead = 1 << 20
	// attrTimeout is how long the kernel trusts names and attributes;
	// layers never change.
	attrTimeout = 24 * time.Hour
)

var errClosed = errors.New("snapshotter is closed")

type mountedLayer struct {
	layer  *layer
	dir    string
	server *fuse.Server
}

// run serves the requests that decide and change mounts until ctx ends. A
// lazy layer stays mounted while a snapshot that is not a lazy layer
// descends from it, or a call holds it. One goroutine changes every mount,
// so a container's Prepare and the unmount of a layer no container uses
// never interleave. Mounts stay in place at exit.
func (s *snapshotter) run(ctx context.Context) {
	defer close(s.done)
	for {
		select {
		case fn := <-s.requests:
			fn()
		case <-ctx.Done():
			return
		}
	}
}

// do runs fn on the mount goroutine and returns its result. Once accepted,
// fn runs to completion.
func (s *snapshotter) do(ctx context.Context, fn func() error) error {
	reply := make(chan error, 1)
	select {
	case s.requests <- func() { reply <- fn() }:
		return <-reply
	case <-s.done:
		return errClosed
	case <-ctx.Done():
		return fmt.Errorf("wait for mounts: %w", ctx.Err())
	}
}

// hold mounts refs and keeps them mounted until release, which also drops
// mounts nothing needs any more.
func (s *snapshotter) hold(ctx context.Context, refs []lazyRef) (func(), error) {
	if len(refs) == 0 {
		return func() {}, nil
	}
	// The release and its reconcile may run after the call's context ended.
	detached := context.WithoutCancel(ctx)
	err := s.do(ctx, func() error {
		for i, r := range refs {
			if _, ok := s.mounted[r.id]; !ok {
				s.dirty = true
			}
			if err := s.mount(r); err != nil { //nolint:contextcheck // a layer's mount and fill outlive the call
				s.unhold(refs[:i])
				s.tidy(detached)
				return err
			}
			s.holds[r.id]++
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return func() {
		_ = s.do(detached, func() error {
			s.unhold(refs)
			s.tidy(detached)
			return nil
		})
	}, nil
}

func (s *snapshotter) unhold(refs []lazyRef) {
	for _, r := range refs {
		if s.holds[r.id]--; s.holds[r.id] <= 0 {
			delete(s.holds, r.id)
		}
	}
}

// tidy reconciles when a mounted layer may be one no snapshot needs.
func (s *snapshotter) tidy(ctx context.Context) {
	if s.dirty {
		s.reconcile(ctx)
	}
}

// reconcileNow mounts every layer a snapshot needs and unmounts the rest.
// An unmount the kernel refuses as busy is retried on the next pass.
func (s *snapshotter) reconcileNow(ctx context.Context) error {
	return s.do(ctx, func() error {
		s.reconcile(ctx)
		return nil
	})
}

func (s *snapshotter) reconcile(ctx context.Context) {
	needed, err := s.needed(ctx)
	if err != nil {
		s.log.ErrorContext(ctx, "listing the layers snapshots need failed", "error", err)
		return
	}
	s.dirty = false
	for _, r := range needed {
		if err := s.mount(r); err != nil { //nolint:contextcheck // a layer's background fetch lives with its mount, not the call
			s.log.ErrorContext(ctx, "mounting a layer failed", "snapshot", r.id, "layer", r.digest, "error", err)
		}
	}
	for id := range s.mounted {
		if _, ok := needed[id]; ok {
			continue
		}
		if s.holds[id] > 0 {
			s.dirty = true
			continue
		}
		if err := s.unmount(id); err != nil {
			s.dirty = true
			s.log.WarnContext(ctx, "unmounting an unused layer failed", "snapshot", id, "error", err)
		}
	}
}

// removeLazy unmounts a lazy layer snapshot and runs remove, which deletes
// it, with no mount change in between.
func (s *snapshotter) removeLazy(ctx context.Context, id string, remove func() error) error {
	return s.do(ctx, func() error {
		if s.holds[id] > 0 {
			return fmt.Errorf("layer snapshot %s is being mounted: %w", id, errdefs.ErrFailedPrecondition)
		}
		if _, ok := s.mounted[id]; ok {
			if err := s.unmount(id); err != nil {
				return fmt.Errorf("layer snapshot %s is in use: %w: %w", id, errdefs.ErrFailedPrecondition, err)
			}
		}
		if err := remove(); err != nil {
			s.reconcile(ctx)
			return err
		}
		return nil
	})
}

func (s *snapshotter) mount(r lazyRef) error {
	if _, ok := s.mounted[r.id]; ok {
		return nil
	}
	dir := filepath.Join(s.root, "snapshots", r.id)
	raw, err := os.ReadFile(filepath.Join(dir, indexFile)) //nolint:gosec // a path under the snapshotter root
	if err != nil {
		return fmt.Errorf("read index of layer %s: %w", r.digest, err)
	}
	ix, err := imagefs.Unmarshal(raw)
	if err != nil {
		return fmt.Errorf("index of layer %s: %w", r.digest, err)
	}
	if ix.Layer != r.digest {
		return fmt.Errorf("%w: snapshot %s holds layer %s, labelled %s", imagefs.ErrInvalidIndex, r.id, ix.Layer, r.digest)
	}
	if span, traced := s.cache.starts.start(r.digest, "snapshotter.mount"); traced {
		defer span.End()
	}
	l := s.cache.mount(ix)
	root := newRoot(l)
	timeout := attrTimeout
	fsDir := filepath.Join(dir, "fs")
	server, err := gofs.Mount(fsDir, root, &gofs.Options{
		MountOptions: fuse.MountOptions{
			AllowOther: true,
			FsName:     string(r.digest),
			Name:       layersource.Snapshotter,
			Options:    []string{"ro", "default_permissions"},
			// Read-only, keeping setuid files and device nodes as a
			// local layer does.
			DirectMount:      true,
			DirectMountFlags: syscall.MS_RDONLY,
			MaxWrite:         maxRead,
			MaxReadAhead:     maxRead,
		},
		EntryTimeout:    &timeout,
		AttrTimeout:     &timeout,
		NegativeTimeout: &timeout,
		NullPermissions: true,
		RootStableAttr:  &gofs.StableAttr{Ino: 1},
		OnAdd:           root.build,
	})
	if err != nil {
		s.cache.unmount(l)
		return fmt.Errorf("mount layer %s: %w", r.digest, err)
	}
	s.mounted[r.id] = &mountedLayer{layer: l, dir: fsDir, server: server}
	return nil
}

// unmount detaches a layer no snapshot uses. It never waits on the FUSE
// connection: a busy mount fails at once and stays, and the server of one
// that went stops in the background.
func (s *snapshotter) unmount(id string) error {
	ml := s.mounted[id]
	if err := unix.Unmount(ml.dir, 0); err != nil {
		return fmt.Errorf("unmount layer %s: %w", ml.layer.digest, err)
	}
	delete(s.mounted, id)
	s.cache.unmount(ml.layer)
	s.teardowns.Go(ml.server.Wait)
	return nil
}

// needed returns the lazy layers some snapshot that is not a lazy layer
// descends from.
func (s *snapshotter) needed(ctx context.Context) (map[string]lazyRef, error) {
	type snapshot struct {
		id   string
		info snapshots.Info
	}
	byName := make(map[string]snapshot)
	err := s.ms.WithTransaction(ctx, false, func(ctx context.Context) error {
		ids, err := storage.IDMap(ctx)
		if err != nil {
			return err //nolint:wrapcheck // wrapped below
		}
		names := make(map[string]string, len(ids))
		for id, name := range ids {
			names[name] = id
		}
		return storage.WalkInfo(ctx, func(_ context.Context, info snapshots.Info) error { //nolint:wrapcheck // wrapped below
			byName[info.Name] = snapshot{id: names[info.Name], info: info}
			return nil
		})
	})
	// A store that never held a snapshot has no bucket yet.
	if err != nil && !errdefs.IsNotFound(err) {
		return nil, fmt.Errorf("read snapshots: %w", err)
	}
	needed := make(map[string]lazyRef)
	seen := make(map[string]bool)
	for _, snap := range byName {
		if _, lazy := lazyRefOf(snap.id, snap.info); lazy {
			continue
		}
		for name := snap.info.Parent; name != "" && !seen[name]; {
			seen[name] = true
			parent, ok := byName[name]
			if !ok {
				break
			}
			if ref, lazy := lazyRefOf(parent.id, parent.info); lazy {
				needed[ref.id] = ref
			}
			name = parent.info.Parent
		}
	}
	return needed, nil
}

// clearStale detaches the mounts a previous process left under root. Their
// FUSE connections died with it, so containers still using them get I/O
// errors, never another layer's files.
func clearStale(root string) error {
	f, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return fmt.Errorf("read mounts: %w", err)
	}
	defer func() { _ = f.Close() }()
	prefix := filepath.Join(root, "snapshots") + "/"
	var stale []string
	lines := bufio.NewScanner(f)
	for lines.Scan() {
		fields := strings.Fields(lines.Text())
		for i, field := range fields {
			if field == "-" && i+1 < len(fields) && len(fields) > 4 {
				if fields[i+1] == fuseType && strings.HasPrefix(fields[4], prefix) {
					stale = append(stale, fields[4])
				}
				break
			}
		}
	}
	if err := lines.Err(); err != nil {
		return fmt.Errorf("read mounts: %w", err)
	}
	for _, path := range stale {
		if err := unix.Unmount(path, unix.MNT_DETACH); err != nil {
			return fmt.Errorf("detach stale layer mount %s: %w", path, err)
		}
	}
	return nil
}

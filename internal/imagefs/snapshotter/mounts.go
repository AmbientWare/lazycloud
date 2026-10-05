package snapshotter

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
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

// lazyRef names a committed lazy layer snapshot.
type lazyRef struct {
	id     string
	digest imagefs.Digest
}

type mountedLayer struct {
	digest imagefs.Digest
	server *fuse.Server
}

// mounts owns the FUSE servers of mounted lazy layers. A layer stays
// mounted while a snapshot that is not a lazy layer descends from it, or a
// call holds it. One goroutine decides and changes every mount, so a
// container's Prepare and the unmount of a layer no container uses never
// interleave.
type mounts struct {
	root       string
	ms         *storage.MetaStore
	frames     *frameCache
	grants     *grants
	http       *http.Client
	metrics    *metrics
	log        *slog.Logger
	allowOther bool

	requests chan func()
	done     chan struct{}

	// Owned by run.
	mounted map[string]*mountedLayer
	holds   map[string]int
}

// run serves requests until ctx ends, then unmounts every layer it can.
func (m *mounts) run(ctx context.Context) {
	defer close(m.done)
	for {
		select {
		case fn := <-m.requests:
			fn()
		case <-ctx.Done():
			for id := range m.mounted {
				if err := m.unmount(id); err != nil {
					m.log.Warn("layer left mounted at exit", "snapshot", id, "error", err)
				}
			}
			return
		}
	}
}

// do runs fn on the mount goroutine and returns its result. Once accepted,
// fn runs to completion.
func (m *mounts) do(ctx context.Context, fn func() error) error {
	reply := make(chan error, 1)
	select {
	case m.requests <- func() { reply <- fn() }:
		return <-reply
	case <-m.done:
		return errClosed
	case <-ctx.Done():
		return fmt.Errorf("wait for mounts: %w", ctx.Err())
	}
}

// hold mounts refs and keeps them mounted until release, which also drops
// mounts nothing needs any more.
func (m *mounts) hold(ctx context.Context, refs []lazyRef) (func(), error) {
	if len(refs) == 0 {
		return func() {}, nil
	}
	err := m.do(ctx, func() error {
		for i, r := range refs {
			if err := m.mount(r); err != nil {
				for _, held := range refs[:i] {
					m.unhold(held.id)
				}
				return err
			}
			m.holds[r.id]++
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return func() {
		_ = m.do(context.WithoutCancel(ctx), func() error {
			for _, r := range refs {
				m.unhold(r.id)
			}
			m.reconcile(ctx)
			return nil
		})
	}, nil
}

func (m *mounts) unhold(id string) {
	if m.holds[id]--; m.holds[id] <= 0 {
		delete(m.holds, id)
	}
}

// reconcile mounts every layer a snapshot needs and unmounts the rest. An
// unmount the kernel refuses as busy is retried on the next pass.
func (m *mounts) reconcileNow(ctx context.Context) error {
	return m.do(ctx, func() error {
		m.reconcile(ctx)
		return nil
	})
}

func (m *mounts) reconcile(ctx context.Context) {
	needed, err := m.needed(ctx)
	if err != nil {
		m.log.ErrorContext(ctx, "list the layers snapshots need", "error", err)
		return
	}
	for _, r := range needed {
		if err := m.mount(r); err != nil {
			m.log.ErrorContext(ctx, "mount layer", "snapshot", r.id, "layer", r.digest, "error", err)
		}
	}
	for id := range m.mounted {
		if _, ok := needed[id]; ok || m.holds[id] > 0 {
			continue
		}
		if err := m.unmount(id); err != nil {
			m.log.WarnContext(ctx, "unmount unused layer", "snapshot", id, "error", err)
		}
	}
}

// removeLazy unmounts a lazy layer snapshot and runs remove, which deletes
// it, with no mount change in between.
func (m *mounts) removeLazy(ctx context.Context, id string, remove func() error) error {
	return m.do(ctx, func() error {
		if m.holds[id] > 0 {
			return fmt.Errorf("layer snapshot %s is being mounted: %w", id, errdefs.ErrFailedPrecondition)
		}
		if _, ok := m.mounted[id]; ok {
			if err := m.unmount(id); err != nil {
				return fmt.Errorf("layer snapshot %s is in use: %w: %w", id, errdefs.ErrFailedPrecondition, err)
			}
		}
		if err := remove(); err != nil {
			m.reconcile(ctx)
			return err
		}
		return nil
	})
}

func (m *mounts) mount(r lazyRef) error {
	if _, ok := m.mounted[r.id]; ok {
		return nil
	}
	dir := filepath.Join(m.root, "snapshots", r.id)
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
	digest := r.digest
	l := &layer{
		digest: digest,
		index:  ix,
		data: imagefs.HTTPObject(m.http, func() string {
			g, _ := m.grants.lookup(digest)
			return g.dataURL
		}),
		frames: m.frames,
		log:    m.log,
		failed: m.metrics.readFailures.Inc,
	}
	root := newRoot(l)
	timeout := attrTimeout
	server, err := gofs.Mount(filepath.Join(dir, "fs"), root, &gofs.Options{
		MountOptions: fuse.MountOptions{
			AllowOther: m.allowOther,
			FsName:     string(digest),
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
		return fmt.Errorf("mount layer %s: %w", digest, err)
	}
	m.mounted[r.id] = &mountedLayer{digest: digest, server: server}
	m.frames.setMounted(digest, 1)
	m.metrics.mountedLayers.Set(float64(len(m.mounted)))
	return nil
}

func (m *mounts) unmount(id string) error {
	ml := m.mounted[id]
	if err := ml.server.Unmount(); err != nil {
		return fmt.Errorf("unmount layer %s: %w", ml.digest, err)
	}
	ml.server.Wait()
	delete(m.mounted, id)
	m.frames.setMounted(ml.digest, -1)
	m.metrics.mountedLayers.Set(float64(len(m.mounted)))
	return nil
}

// needed returns the lazy layers some snapshot that is not a lazy layer
// descends from.
func (m *mounts) needed(ctx context.Context) (map[string]lazyRef, error) {
	type snapshot struct {
		id   string
		info snapshots.Info
	}
	byName := make(map[string]snapshot)
	err := m.ms.WithTransaction(ctx, false, func(ctx context.Context) error {
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
	for _, s := range byName {
		if isLazy(s.info) {
			continue
		}
		for name := s.info.Parent; name != "" && !seen[name]; {
			seen[name] = true
			parent, ok := byName[name]
			if !ok {
				break
			}
			if isLazy(parent.info) {
				needed[parent.id] = lazyRef{id: parent.id, digest: imagefs.Digest(parent.info.Labels[snapshots.LabelSnapshotDiffID])}
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

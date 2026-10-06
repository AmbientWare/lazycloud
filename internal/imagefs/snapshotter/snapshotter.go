// Package snapshotter is the host service that gives containerd, and so
// Docker, lazily read image layers. It is containerd's overlay snapshotter
// plus lazy layers: a layer pulled with layersource.LazyLabel becomes a
// committed snapshot holding only the layer's index, mounted over FUSE
// while a container uses it, its files read by frame from the layer store
// through the URLs the agent grants, and kept in a bounded local cache.
// Every other snapshot is a plain overlay snapshot.
//
// The process holding the FUSE mounts must outlive every container using
// them, so it runs as its own service that agent updates never restart. It
// has no database access and no store credential.
package snapshotter

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"sync"

	"github.com/containerd/containerd/v2/core/mount"
	"github.com/containerd/containerd/v2/core/snapshots"
	"github.com/containerd/containerd/v2/core/snapshots/storage"
	"github.com/containerd/containerd/v2/plugins/snapshots/overlay"
	"github.com/containerd/errdefs"
	"go.opentelemetry.io/otel/attribute"
	oteltrace "go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// indexFile is a lazy layer snapshot's stored index, beside the fs
	// directory its FUSE filesystem mounts on.
	indexFile = "index"
	// prepareSuffix names the active snapshot a lazy layer is committed
	// from.
	prepareSuffix = "/lazy-prepare"
)

// Config configures the snapshotter Serve runs.
type Config struct {
	// Root holds the snapshot metadata, the snapshots and the frame cache.
	Root string
	// CacheBytes bounds the frame cache on disk.
	CacheBytes int64
	// Fetches bounds the frames read from the store at once.
	Fetches int
	// FillBytes is the largest layer, in uncompressed bytes, whose frames
	// are all fetched in the background once it is mounted; zero fills
	// none.
	FillBytes int64
	HTTP      *http.Client
	Logger    *slog.Logger
	Tracer    oteltrace.Tracer
}

// snapshotter is a containerd snapshotter with lazy layers. One goroutine,
// run, owns the mounts (mounts.go).
type snapshotter struct {
	root    string
	ms      *storage.MetaStore
	overlay snapshots.Snapshotter
	cache   *frameCache
	log     *slog.Logger
	cancel  context.CancelFunc

	requests chan func()
	done     chan struct{}
	// teardowns waits for the servers of unmounted layers to stop.
	teardowns sync.WaitGroup
	// Owned by run.
	mounted map[string]*mountedLayer
	holds   map[string]int
	// dirty is set while a mounted layer may be one no snapshot needs.
	dirty bool
}

var (
	_ snapshots.Snapshotter = (*snapshotter)(nil)
	_ snapshots.Cleaner     = (*snapshotter)(nil)
)

// newSnapshotter opens the snapshotter at cfg.Root, detaches mounts a
// previous process left and mounts the layers existing snapshots need.
func newSnapshotter(ctx context.Context, cfg Config) (*snapshotter, error) {
	if err := os.MkdirAll(cfg.Root, 0o700); err != nil {
		return nil, fmt.Errorf("create snapshotter root: %w", err)
	}
	if err := clearStale(cfg.Root); err != nil {
		return nil, err
	}
	life, cancel := context.WithCancel(context.WithoutCancel(ctx))
	cache, err := newFrameCache(life, filepath.Join(cfg.Root, "cache"), cfg)
	if err != nil {
		cancel()
		return nil, err
	}
	ms, err := storage.NewMetaStore(filepath.Join(cfg.Root, "metadata.db"))
	if err != nil {
		cancel()
		return nil, fmt.Errorf("open snapshot metadata: %w", err)
	}
	ov, err := overlay.NewSnapshotter(cfg.Root, overlay.WithMetaStore(ms))
	if err != nil {
		cancel()
		_ = ms.Close()
		return nil, fmt.Errorf("open overlay snapshotter: %w", err)
	}
	s := &snapshotter{
		root: cfg.Root, ms: ms, overlay: ov, cache: cache, log: cfg.Logger, cancel: cancel,
		requests: make(chan func()),
		done:     make(chan struct{}),
		mounted:  make(map[string]*mountedLayer),
		holds:    make(map[string]int),
	}
	cache.background.Go(func() { cache.starts.run(life) })
	go s.run(life)
	if err := s.reconcileNow(ctx); err != nil {
		_ = s.Close()
		return nil, err
	}
	return s, nil
}

// Close stops the snapshotter's work and closes its metadata. Mounts stay
// in place: containers may still read them while the process lives, and
// the next start detaches what is left.
func (s *snapshotter) Close() error {
	s.cancel()
	<-s.done
	s.teardowns.Wait()
	s.cache.background.Wait()
	return s.overlay.Close() //nolint:wrapcheck // the metadata store's own error
}

func (s *snapshotter) Stat(ctx context.Context, key string) (snapshots.Info, error) {
	return s.overlay.Stat(ctx, key) //nolint:wrapcheck // containerd's typed errors pass through
}

func (s *snapshotter) Update(ctx context.Context, info snapshots.Info, fieldpaths ...string) (snapshots.Info, error) {
	return s.overlay.Update(ctx, info, fieldpaths...) //nolint:wrapcheck // containerd's typed errors pass through
}

func (s *snapshotter) Usage(ctx context.Context, key string) (snapshots.Usage, error) {
	return s.overlay.Usage(ctx, key) //nolint:wrapcheck // containerd's typed errors pass through
}

func (s *snapshotter) Walk(ctx context.Context, fn snapshots.WalkFunc, filters ...string) error {
	return s.overlay.Walk(ctx, fn, filters...) //nolint:wrapcheck // containerd's typed errors pass through
}

func (s *snapshotter) Commit(ctx context.Context, name, key string, opts ...snapshots.Opt) error {
	return s.overlay.Commit(ctx, name, key, opts...) //nolint:wrapcheck // containerd's typed errors pass through
}

// Prepare makes a layer present lazily when the pull marked it, and
// otherwise prepares an overlay snapshot over its parent's layers.
func (s *snapshotter) Prepare(ctx context.Context, key, parent string, opts ...snapshots.Opt) ([]mount.Mount, error) {
	var info snapshots.Info
	for _, opt := range opts {
		if err := opt(&info); err != nil {
			return nil, err
		}
	}
	if info.Labels[layersource.LazyLabel] != "" {
		return nil, s.prepareLazy(ctx, key, parent, info.Labels)
	}
	return s.withLayers(ctx, parent, func() ([]mount.Mount, error) {
		return s.overlay.Prepare(ctx, key, parent, opts...)
	})
}

func (s *snapshotter) View(ctx context.Context, key, parent string, opts ...snapshots.Opt) ([]mount.Mount, error) {
	return s.withLayers(ctx, parent, func() ([]mount.Mount, error) {
		return s.overlay.View(ctx, key, parent, opts...)
	})
}

func (s *snapshotter) Mounts(ctx context.Context, key string) ([]mount.Mount, error) {
	return s.withLayers(ctx, key, func() ([]mount.Mount, error) {
		return s.overlay.Mounts(ctx, key)
	})
}

// withLayers runs fn with the lazy layers below name mounted. The snapshot
// fn creates or names then keeps them mounted.
func (s *snapshotter) withLayers(ctx context.Context, name string, fn func() ([]mount.Mount, error)) ([]mount.Mount, error) {
	refs, err := s.lazyAncestors(ctx, name)
	if err != nil {
		return nil, err
	}
	release, err := s.hold(ctx, refs)
	if err != nil {
		return nil, err
	}
	defer release()
	return fn()
}

// Remove deletes a snapshot. A lazy layer is unmounted first; any other
// removal unmounts the layers no snapshot needs any more.
func (s *snapshotter) Remove(ctx context.Context, key string) error {
	var (
		ref  lazyRef
		lazy bool
	)
	if err := s.ms.WithTransaction(ctx, false, func(ctx context.Context) error {
		id, info, _, err := storage.GetInfo(ctx, key)
		ref, lazy = lazyRefOf(id, info)
		return err //nolint:wrapcheck // containerd's typed errors pass through
	}); err != nil {
		return err //nolint:wrapcheck // containerd's typed errors pass through
	}
	if lazy {
		return s.removeLazy(ctx, ref.id, func() error { return s.overlay.Remove(ctx, key) })
	}
	if err := s.overlay.Remove(ctx, key); err != nil {
		return err //nolint:wrapcheck // containerd's typed errors pass through
	}
	if err := s.reconcileNow(ctx); err != nil {
		s.log.WarnContext(ctx, "unmounting unused layers failed", "error", err)
	}
	return nil
}

// Cleanup removes abandoned snapshot directories and unmounts layers no
// snapshot needs.
func (s *snapshotter) Cleanup(ctx context.Context) error {
	if err := s.overlay.(snapshots.Cleaner).Cleanup(ctx); err != nil { //nolint:forcetypeassert // overlay cleans up
		return err //nolint:wrapcheck // containerd's typed errors pass through
	}
	return s.reconcileNow(ctx)
}

// prepareLazy commits a lazy layer snapshot for the layer the labels name
// and reports it already exists, the remote snapshot protocol that makes
// containerd skip the layer's download. The snapshot is committed under key
// itself with the target labels, where containerd's Walk finds it.
func (s *snapshotter) prepareLazy(ctx context.Context, key, parent string, labels map[string]string) error {
	digest := imagefs.Digest(labels[snapshots.LabelSnapshotDiffID])
	if labels[snapshots.LabelSnapshotRef] == "" || digest.Check() != nil {
		return fmt.Errorf("lazy layer %q needs %s and %s labels: %w", key, snapshots.LabelSnapshotRef, snapshots.LabelSnapshotDiffID, errdefs.ErrInvalidArgument)
	}
	span, traced := s.cache.starts.start(digest, "snapshotter.prepare_layer")
	raw, ix, err := s.cache.fetchIndex(ctx, digest)
	if traced {
		span.SetAttributes(attribute.Int("lazycloud.index_bytes", len(raw)), attribute.Int("lazycloud.frames", len(ix.Frames)))
		telemetry.Fail(span, err)
	}
	if errors.Is(err, errNoGrant) {
		return fmt.Errorf("layer %s cannot be mounted: %w: %w", digest, err, errdefs.ErrFailedPrecondition)
	}
	if err != nil {
		return err
	}
	usage := snapshots.Usage{Size: ix.StreamSize, Inodes: int64(len(ix.Entries))}
	dirs := filepath.Join(s.root, "snapshots")
	var tmp, final string
	err = s.ms.WithTransaction(ctx, true, func(ctx context.Context) error {
		var err error
		if tmp, err = os.MkdirTemp(dirs, "new-"); err != nil {
			return fmt.Errorf("create snapshot directory: %w", err)
		}
		if err := os.Mkdir(filepath.Join(tmp, "fs"), 0o755); err != nil { //nolint:gosec // the mount point overlayfs reads
			return fmt.Errorf("create snapshot directory: %w", err)
		}
		if err := os.WriteFile(filepath.Join(tmp, indexFile), raw, 0o600); err != nil {
			return fmt.Errorf("store layer index: %w", err)
		}
		active, err := storage.CreateSnapshot(ctx, snapshots.KindActive, key+prepareSuffix, parent, snapshots.WithLabels(labels))
		if err != nil {
			return err //nolint:wrapcheck // containerd's typed errors pass through
		}
		final = filepath.Join(dirs, active.ID)
		if err := os.Rename(tmp, final); err != nil {
			return fmt.Errorf("place snapshot directory: %w", err)
		}
		tmp = ""
		_, err = storage.CommitActive(ctx, key+prepareSuffix, key, usage, snapshots.WithLabels(labels))
		return err //nolint:wrapcheck // containerd's typed errors pass through
	})
	if err != nil {
		for _, dir := range []string{tmp, final} {
			if dir != "" {
				_ = os.RemoveAll(dir)
			}
		}
		return fmt.Errorf("record lazy layer %s: %w", digest, err)
	}
	return fmt.Errorf("layer %s is lazily present: %w", digest, errdefs.ErrAlreadyExists)
}

// lazyAncestors returns name and its ancestors that are lazy layers.
func (s *snapshotter) lazyAncestors(ctx context.Context, name string) ([]lazyRef, error) {
	var refs []lazyRef
	err := s.ms.WithTransaction(ctx, false, func(ctx context.Context) error {
		for name != "" {
			id, info, _, err := storage.GetInfo(ctx, name)
			if err != nil {
				return err //nolint:wrapcheck // containerd's typed errors pass through
			}
			if ref, ok := lazyRefOf(id, info); ok {
				refs = append(refs, ref)
			}
			name = info.Parent
		}
		return nil
	})
	if err != nil {
		return nil, err //nolint:wrapcheck // containerd's typed errors pass through
	}
	return refs, nil
}

// lazyRef names a committed lazy layer snapshot.
type lazyRef struct {
	id     string
	digest imagefs.Digest
}

// lazyRefOf returns the snapshot id with info if it is a lazy layer.
func lazyRefOf(id string, info snapshots.Info) (lazyRef, bool) {
	if info.Kind != snapshots.KindCommitted || info.Labels[layersource.LazyLabel] == "" {
		return lazyRef{}, false
	}
	return lazyRef{id: id, digest: imagefs.Digest(info.Labels[snapshots.LabelSnapshotDiffID])}, true
}

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
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"time"

	"github.com/containerd/containerd/v2/core/mount"
	"github.com/containerd/containerd/v2/core/snapshots"
	"github.com/containerd/containerd/v2/core/snapshots/storage"
	"github.com/containerd/containerd/v2/plugins/snapshots/overlay"
	"github.com/containerd/errdefs"
	"github.com/prometheus/client_golang/prometheus"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

const (
	// indexFile is a lazy layer snapshot's stored index, beside the fs
	// directory its FUSE filesystem mounts on.
	indexFile = "index"
	// maxIndexBytes bounds a stored index; the index of a 1 GiB layer is
	// about 400 KB.
	maxIndexBytes = 64 << 20
	// prepareSuffix names the active snapshot a lazy layer is committed
	// from.
	prepareSuffix = "/lazy-prepare"
)

// Config configures a Snapshotter.
type Config struct {
	// Root holds the snapshot metadata, the snapshots and the frame cache.
	Root string
	// CacheBytes bounds the frame cache on disk.
	CacheBytes int64
	// Fetches bounds the frames read from the store at once.
	Fetches int
	// AllowOther lets users other than the snapshotter's read the mounts.
	// It needs root, which production has.
	AllowOther bool
	HTTP       *http.Client
	Registry   prometheus.Registerer
	Logger     *slog.Logger
}

// Snapshotter is a containerd snapshotter with lazy layers.
type Snapshotter struct {
	root    string
	ms      *storage.MetaStore
	overlay snapshots.Snapshotter
	grants  *grants
	mounts  *mounts
	http    *http.Client
	log     *slog.Logger
	cancel  context.CancelFunc
}

var (
	_ snapshots.Snapshotter = (*Snapshotter)(nil)
	_ snapshots.Cleaner     = (*Snapshotter)(nil)
)

// New opens the snapshotter at cfg.Root, detaches mounts a previous
// process left and mounts the layers existing snapshots need. Close stops
// it.
func New(ctx context.Context, cfg Config) (*Snapshotter, error) {
	if err := os.MkdirAll(cfg.Root, 0o700); err != nil {
		return nil, fmt.Errorf("create snapshotter root: %w", err)
	}
	if err := clearStale(cfg.Root); err != nil {
		return nil, err
	}
	m, err := newMetrics(cfg.Registry)
	if err != nil {
		return nil, err
	}
	g := newGrants(time.Now)
	frames, err := newFrameCache(filepath.Join(cfg.Root, "cache"), cfg.CacheBytes, cfg.Fetches, g, m, cfg.Logger)
	if err != nil {
		return nil, err
	}
	ms, err := storage.NewMetaStore(filepath.Join(cfg.Root, "metadata.db"))
	if err != nil {
		return nil, fmt.Errorf("open snapshot metadata: %w", err)
	}
	ov, err := overlay.NewSnapshotter(cfg.Root, overlay.WithMetaStore(ms))
	if err != nil {
		_ = ms.Close()
		return nil, fmt.Errorf("open overlay snapshotter: %w", err)
	}
	life, cancel := context.WithCancel(context.WithoutCancel(ctx))
	s := &Snapshotter{
		root: cfg.Root, ms: ms, overlay: ov, grants: g, http: cfg.HTTP, log: cfg.Logger, cancel: cancel,
		mounts: &mounts{
			root: cfg.Root, ms: ms, frames: frames, grants: g, http: cfg.HTTP, metrics: m, log: cfg.Logger,
			allowOther: cfg.AllowOther,
			requests:   make(chan func()),
			done:       make(chan struct{}),
			mounted:    make(map[string]*mountedLayer),
			holds:      make(map[string]int),
		},
	}
	go s.mounts.run(life)
	if err := s.mounts.reconcileNow(ctx); err != nil {
		_ = s.Close()
		return nil, err
	}
	return s, nil
}

// Close unmounts every layer no container holds and closes the metadata.
func (s *Snapshotter) Close() error {
	s.cancel()
	<-s.mounts.done
	return s.overlay.Close() //nolint:wrapcheck // the metadata store's own error
}

func (s *Snapshotter) Stat(ctx context.Context, key string) (snapshots.Info, error) {
	return s.overlay.Stat(ctx, key) //nolint:wrapcheck // containerd's typed errors pass through
}

func (s *Snapshotter) Update(ctx context.Context, info snapshots.Info, fieldpaths ...string) (snapshots.Info, error) {
	return s.overlay.Update(ctx, info, fieldpaths...) //nolint:wrapcheck // containerd's typed errors pass through
}

func (s *Snapshotter) Usage(ctx context.Context, key string) (snapshots.Usage, error) {
	return s.overlay.Usage(ctx, key) //nolint:wrapcheck // containerd's typed errors pass through
}

func (s *Snapshotter) Walk(ctx context.Context, fn snapshots.WalkFunc, filters ...string) error {
	return s.overlay.Walk(ctx, fn, filters...) //nolint:wrapcheck // containerd's typed errors pass through
}

func (s *Snapshotter) Commit(ctx context.Context, name, key string, opts ...snapshots.Opt) error {
	return s.overlay.Commit(ctx, name, key, opts...) //nolint:wrapcheck // containerd's typed errors pass through
}

// Prepare makes a layer present lazily when the pull marked it, and
// otherwise prepares an overlay snapshot over its parent's layers.
func (s *Snapshotter) Prepare(ctx context.Context, key, parent string, opts ...snapshots.Opt) ([]mount.Mount, error) {
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

func (s *Snapshotter) View(ctx context.Context, key, parent string, opts ...snapshots.Opt) ([]mount.Mount, error) {
	return s.withLayers(ctx, parent, func() ([]mount.Mount, error) {
		return s.overlay.View(ctx, key, parent, opts...)
	})
}

func (s *Snapshotter) Mounts(ctx context.Context, key string) ([]mount.Mount, error) {
	return s.withLayers(ctx, key, func() ([]mount.Mount, error) {
		return s.overlay.Mounts(ctx, key)
	})
}

// withLayers runs fn with the lazy layers below name mounted. The snapshot
// fn creates or names then keeps them mounted.
func (s *Snapshotter) withLayers(ctx context.Context, name string, fn func() ([]mount.Mount, error)) ([]mount.Mount, error) {
	refs, err := s.lazyAncestors(ctx, name)
	if err != nil {
		return nil, err
	}
	release, err := s.mounts.hold(ctx, refs)
	if err != nil {
		return nil, err
	}
	defer release()
	return fn()
}

// Remove deletes a snapshot. A lazy layer is unmounted first; any other
// removal unmounts the layers no snapshot needs any more.
func (s *Snapshotter) Remove(ctx context.Context, key string) error {
	var (
		id   string
		info snapshots.Info
	)
	if err := s.ms.WithTransaction(ctx, false, func(ctx context.Context) error {
		var err error
		id, info, _, err = storage.GetInfo(ctx, key)
		return err //nolint:wrapcheck // containerd's typed errors pass through
	}); err != nil {
		return err //nolint:wrapcheck // containerd's typed errors pass through
	}
	if isLazy(info) {
		return s.mounts.removeLazy(ctx, id, func() error { return s.overlay.Remove(ctx, key) })
	}
	if err := s.overlay.Remove(ctx, key); err != nil {
		return err //nolint:wrapcheck // containerd's typed errors pass through
	}
	if err := s.mounts.reconcileNow(ctx); err != nil {
		s.log.WarnContext(ctx, "unmount unused layers", "error", err)
	}
	return nil
}

// Cleanup removes abandoned snapshot directories and unmounts layers no
// snapshot needs.
func (s *Snapshotter) Cleanup(ctx context.Context) error {
	if err := s.overlay.(snapshots.Cleaner).Cleanup(ctx); err != nil { //nolint:forcetypeassert // overlay cleans up
		return err //nolint:wrapcheck // containerd's typed errors pass through
	}
	return s.mounts.reconcileNow(ctx)
}

// prepareLazy commits a lazy layer snapshot for the layer the labels name
// and reports it already exists, the remote snapshot protocol that makes
// containerd skip the layer's download. The snapshot is committed under key
// itself with the target labels, where containerd's Walk finds it.
func (s *Snapshotter) prepareLazy(ctx context.Context, key, parent string, labels map[string]string) error {
	digest := imagefs.Digest(labels[snapshots.LabelSnapshotDiffID])
	if labels[snapshots.LabelSnapshotRef] == "" || !layerDigest.MatchString(string(digest)) {
		return fmt.Errorf("lazy layer %q needs %s and %s labels: %w", key, snapshots.LabelSnapshotRef, snapshots.LabelSnapshotDiffID, errdefs.ErrInvalidArgument)
	}
	g, ok := s.grants.lookup(digest)
	if !ok {
		return fmt.Errorf("layer %s has no live grant from the agent, so it cannot be mounted: %w", digest, errdefs.ErrFailedPrecondition)
	}
	raw, ix, err := s.fetchIndex(ctx, digest, g)
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

// fetchIndex reads and checks a layer's stored index, with the same bounded
// retries as frames.
func (s *Snapshotter) fetchIndex(ctx context.Context, digest imagefs.Digest, g grant) ([]byte, imagefs.Index, error) {
	var err error
	for attempt := range fetchAttempts {
		if attempt > 0 {
			if serr := sleep(ctx, backoff(attempt)); serr != nil {
				break
			}
			if g, _ = s.grants.lookup(digest); g.indexURL == "" {
				return nil, imagefs.Index{}, fmt.Errorf("layer %s: %w", digest, errNoGrant)
			}
		}
		var raw []byte
		raw, err = s.getIndex(ctx, g.indexURL)
		if err == nil {
			var ix imagefs.Index
			if ix, err = imagefs.Unmarshal(raw); err == nil && ix.Layer != digest {
				err = fmt.Errorf("%w: the index granted for layer %s is layer %s's", imagefs.ErrInvalidIndex, digest, ix.Layer)
			}
			if err == nil {
				return raw, ix, nil
			}
			break
		}
		if !retryable(err) {
			break
		}
	}
	return nil, imagefs.Index{}, fmt.Errorf("index of layer %s: %w", digest, err)
}

func (s *Snapshotter) getIndex(ctx context.Context, rawURL string) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, errors.New("read index: unusable URL")
	}
	response, err := s.http.Do(request)
	if err != nil {
		var ue *url.Error
		if errors.As(err, &ue) {
			// The URL's query is its signature.
			err = ue.Err
		}
		return nil, fmt.Errorf("read index: %w", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(response.Body, 512))
		return nil, &imagefs.StatusError{StatusCode: response.StatusCode, Body: string(body)}
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxIndexBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read index: %w", err)
	}
	if len(raw) > maxIndexBytes {
		return nil, fmt.Errorf("%w: index exceeds %d bytes", imagefs.ErrInvalidIndex, maxIndexBytes)
	}
	return raw, nil
}

// lazyAncestors returns name and its ancestors that are lazy layers.
func (s *Snapshotter) lazyAncestors(ctx context.Context, name string) ([]lazyRef, error) {
	var refs []lazyRef
	err := s.ms.WithTransaction(ctx, false, func(ctx context.Context) error {
		for name != "" {
			id, info, _, err := storage.GetInfo(ctx, name)
			if err != nil {
				return err //nolint:wrapcheck // containerd's typed errors pass through
			}
			if isLazy(info) {
				refs = append(refs, lazyRef{id: id, digest: imagefs.Digest(info.Labels[snapshots.LabelSnapshotDiffID])})
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

// isLazy reports whether info is a lazy layer snapshot.
func isLazy(info snapshots.Info) bool {
	return info.Kind == snapshots.KindCommitted && info.Labels[layersource.LazyLabel] != ""
}

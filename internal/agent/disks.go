package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/moby/moby/api/types/mount"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/diskengine"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// Durable disks: the agent leases each disk a container declares, restores
// it with the disk engine, bind-mounts it, publishes a generation every
// publishEvery and, once the container exits, publishes the last one,
// detaches and releases the lease. Until the release the server reports the
// disk as saving and no other container can take it.
const (
	publishEvery = 2 * time.Minute
	// compactAfter is how many committed layers the engine folds together.
	compactAfter = 8
	// acquireWait bounds how long a start waits for another holder to
	// release the disk.
	acquireWait = 5 * time.Minute
	// diskMinFree is the space restores leave free on the host.
	diskMinFree = 20 << 30
	// disksFile records a container's leases so a restarted agent can
	// publish and release them.
	disksFile = "disks.json"
)

// heldDisk is one lease of a container.
type heldDisk struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Workspace string `json:"workspace"`
	Token     []byte `json:"token"`
	// Layers counts generations committed since the last compaction.
	Layers int `json:"layers"`
}

// diskSet is a container's leased disks; mu serializes publishing.
type diskSet struct {
	mu    sync.Mutex
	disks []*heldDisk
}

func (a *Agent) diskMount(container, disk string) string {
	return filepath.Join(a.cfg.StateDir, "disks", "mounts", container, disk)
}

func (a *Agent) diskStore(workspace string) (diskengine.Store, error) {
	loc, err := a.volumes.location(workspace)
	if err != nil {
		return diskengine.Store{}, err
	}
	return diskengine.Store{
		Endpoint: loc.Endpoint, Region: loc.Region, Bucket: loc.Bucket, ForcePathStyle: true,
		Credentials: func(context.Context) (diskengine.Credentials, error) {
			data, err := os.ReadFile(filepath.Join(a.volumes.storageDir(workspace), "credentials.json"))
			if err != nil {
				return diskengine.Credentials{}, fmt.Errorf("read storage credentials: %w", err)
			}
			var c processCredentials
			if err := json.Unmarshal(data, &c); err != nil {
				return diskengine.Credentials{}, fmt.Errorf("decode storage credentials: %w", err)
			}
			expires, err := time.Parse(time.RFC3339, c.Expiration)
			if err != nil {
				return diskengine.Credentials{}, fmt.Errorf("storage credential expiry: %w", err)
			}
			return diskengine.Credentials{AccessKeyID: c.AccessKeyID, SecretAccessKey: c.SecretAccessKey, SessionToken: c.SessionToken, ExpiresAt: expires}, nil
		},
	}, nil
}

// attachDisks leases and attaches the container's disks and returns their
// bind mounts. Leases are recorded before attaching, so a failure part way
// is released by cleanup like any other exit.
func (c *container) attachDisks(ctx context.Context, specs []*hostproto.DiskAttachment) ([]mount.Mount, []string, error) {
	if len(specs) == 0 {
		return nil, nil, nil
	}
	if c.a.diskErr != nil {
		return nil, nil, fmt.Errorf("this host cannot attach disks: %w", c.a.diskErr)
	}
	var binds []mount.Mount
	var workspaces []string
	for _, spec := range specs {
		if spec.GetMountPath() == "/" || !filepath.IsAbs(spec.GetMountPath()) {
			return nil, nil, fmt.Errorf("disk %s: Docker hosts mount disks at an absolute directory, not %q", spec.GetName(), spec.GetMountPath())
		}
		lease, err := c.acquire(ctx, spec.GetName())
		if err != nil {
			return nil, nil, err
		}
		held := &heldDisk{ID: lease.GetDiskId(), Name: spec.GetName(), Workspace: lease.GetWorkspaceId(), Token: lease.GetLeaseToken()}
		c.disks.mu.Lock()
		c.disks.disks = append(c.disks.disks, held)
		err = c.saveDisks()
		c.disks.mu.Unlock()
		if err != nil {
			return nil, nil, err
		}
		if err := c.a.volumes.waitGrant(ctx, held.Workspace); err != nil {
			return nil, nil, err
		}
		store, err := c.a.diskStore(held.Workspace)
		if err != nil {
			return nil, nil, err
		}
		chain := make([]diskengine.Generation, len(lease.GetChain()))
		for n, g := range lease.GetChain() {
			chain[n] = diskengine.Generation{Generation: g.GetGeneration(), ManifestKey: g.GetManifestKey(), ManifestSHA256: g.GetManifestSha256()}
		}
		request := diskengine.AttachRequest{
			DiskID: held.ID, SizeBytes: lease.GetSizeBytes(), Chain: chain,
			Mountpoint: c.a.diskMount(c.id, held.ID), Store: store, MinFreeBytes: diskMinFree,
		}
		_, err = c.a.diskEngine.Attach(ctx, request)
		if errors.Is(err, diskengine.ErrInsufficientSpace) && c.a.evictDisks(ctx) {
			_, err = c.a.diskEngine.Attach(ctx, request)
		}
		if err != nil {
			return nil, nil, fmt.Errorf("attach disk %s: %w", spec.GetName(), err)
		}
		binds = append(binds, mount.Mount{Type: mount.TypeBind, Source: request.Mountpoint, Target: spec.GetMountPath()})
		workspaces = append(workspaces, held.Workspace)
	}
	c.a.goOwned(func(context.Context) { c.publishLoop(c.work) }) //nolint:contextcheck // Publishing lasts as long as the container's work.
	return binds, workspaces, nil
}

// acquire takes a lease, waiting while another container still holds the
// disk.
func (c *container) acquire(ctx context.Context, name string) (*hostproto.AcquireDiskResponse, error) {
	deadline := time.Now().Add(acquireWait)
	delay := time.Second
	for {
		lease, err := c.a.host.AcquireDisk(ctx, &hostproto.AcquireDiskRequest{ContainerId: c.id, Name: name})
		if err == nil {
			return lease, nil
		}
		if (status.Code(err) != codes.Aborted && !retryable(err)) || time.Now().After(deadline) {
			return nil, fmt.Errorf("acquire disk %s: %w", name, err)
		}
		c.log.Info("disk is held elsewhere; waiting", "disk", name, "error", err)
		if !sleep(ctx, delay) {
			return nil, fmt.Errorf("acquire disk %s: %w", name, ctx.Err())
		}
		delay = min(2*delay, 15*time.Second)
	}
}

func (c *container) saveDisks() error {
	data, err := json.Marshal(c.disks.disks)
	if err != nil {
		return fmt.Errorf("encode disk leases: %w", err)
	}
	if err := os.MkdirAll(c.dir, 0o700); err != nil {
		return fmt.Errorf("create container directory: %w", err)
	}
	if err := writeFileAtomic(filepath.Join(c.dir, disksFile), data, 0o600); err != nil {
		return fmt.Errorf("record disk leases: %w", err)
	}
	return nil
}

// loadDisks restores the leases of an adopted container.
func (c *container) loadDisks() error {
	data, err := os.ReadFile(filepath.Join(c.dir, disksFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read disk leases: %w", err)
	}
	return json.Unmarshal(data, &c.disks.disks) //nolint:wrapcheck // A corrupt lease file names itself.
}

func (c *container) publishLoop(ctx context.Context) {
	ticker := time.NewTicker(publishEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		for _, d := range c.heldDisks() {
			if err := c.publish(ctx, d); err != nil && ctx.Err() == nil {
				c.log.Warn("publishing disk failed; retrying next round", "disk", d.Name, "error", err)
			}
		}
	}
}

func (c *container) heldDisks() []*heldDisk {
	c.disks.mu.Lock()
	defer c.disks.mu.Unlock()
	return append([]*heldDisk(nil), c.disks.disks...)
}

// publish uploads every unpublished layer of d and records each with the
// server before committing it locally, so a lost reply is replayed.
func (c *container) publish(ctx context.Context, d *heldDisk) error {
	c.disks.mu.Lock()
	defer c.disks.mu.Unlock()
	store, err := c.a.diskStore(d.Workspace)
	if err != nil {
		return err
	}
	for {
		published, err := c.a.diskEngine.Publish(ctx, d.ID, store)
		if err != nil {
			return fmt.Errorf("publish: %w", err)
		}
		if published == nil {
			return nil
		}
		if _, err := c.a.host.RecordDiskGeneration(ctx, &hostproto.RecordDiskGenerationRequest{
			ContainerId: c.id, DiskId: d.ID, LeaseToken: d.Token,
			Generation: published.Generation, ParentGeneration: published.ParentGeneration,
			ManifestKey: published.ManifestKey, ManifestSha256: published.ManifestSHA256,
			AddedBytes: published.AddedBytes, Flat: published.Flat,
		}); err != nil {
			return fmt.Errorf("record generation %d: %w", published.Generation, err)
		}
		if err := c.a.diskEngine.CommitPublished(d.ID, published.Generation); err != nil { //nolint:contextcheck // A local state write.
			return fmt.Errorf("commit generation %d: %w", published.Generation, err)
		}
		d.Layers++
		if published.Flat {
			chain := []diskengine.Generation{{Generation: published.Generation, ManifestKey: published.ManifestKey, ManifestSHA256: published.ManifestSHA256}}
			removed, err := c.a.diskEngine.Collect(ctx, d.ID, store, chain)
			if err != nil {
				return fmt.Errorf("collect: %w", err)
			}
			if _, err := c.a.host.RecordDiskCollection(ctx, &hostproto.RecordDiskCollectionRequest{
				ContainerId: c.id, DiskId: d.ID, LeaseToken: d.Token, RemovedBytes: removed, BaseGeneration: published.Generation,
			}); err != nil {
				return fmt.Errorf("record collection: %w", err)
			}
		}
		if d.Layers >= compactAfter {
			if err := c.a.diskEngine.Compact(ctx, d.ID); err != nil {
				return fmt.Errorf("compact: %w", err)
			}
			d.Layers = 0
		}
	}
}

// releaseDisks publishes each disk's last generation, detaches it and ends
// its lease. It runs once the container's exit is reported; a disk that
// fails stays held, so a later agent run can retry from the lease file.
func (c *container) releaseDisks(ctx context.Context) bool {
	if len(c.heldDisks()) == 0 {
		c.disks.mu.Lock()
		err := c.loadDisks()
		c.disks.mu.Unlock()
		if err != nil {
			c.log.Error("reading disk leases failed", "error", err)
			return false
		}
	}
	ok := true
	for _, d := range c.heldDisks() {
		err := c.publish(ctx, d)
		if err == nil || errors.Is(err, diskengine.ErrNotAttached) {
			err = c.a.diskEngine.Detach(ctx, d.ID)
		}
		if err == nil {
			_, err = c.a.host.ReleaseDisk(ctx, &hostproto.ReleaseDiskRequest{ContainerId: c.id, DiskId: d.ID, LeaseToken: d.Token})
			if status.Code(err) == codes.FailedPrecondition {
				// The server already ended the lease, as after host loss.
				err = nil
			}
		}
		if err != nil {
			c.log.Error("releasing disk failed", "disk", d.Name, "error", err)
			ok = false
		}
	}
	return ok
}

// evictDisks drops detached disks whose writes are all published, oldest
// first, and reports whether it freed any.
func (a *Agent) evictDisks(ctx context.Context) bool {
	disks, err := a.diskEngine.List(ctx)
	if err != nil {
		a.log.Warn("listing cached disks failed", "error", err)
		return false
	}
	freed := false
	for _, d := range disks {
		if d.Attached || d.Unpublished {
			continue
		}
		if err := a.diskEngine.Evict(d.DiskID); err != nil { //nolint:contextcheck // A local file removal.
			a.log.Warn("evicting a cached disk failed", "disk_id", d.DiskID, "error", err)
			continue
		}
		freed = true
	}
	return freed
}

// recoverDisks seals disks a dead agent left attached whose containers no
// longer run, so their writes publish before release.
func (a *Agent) recoverDisks(ctx context.Context) {
	if a.diskErr != nil {
		return
	}
	disks, err := a.diskEngine.List(ctx)
	if err != nil {
		a.log.Warn("listing cached disks failed", "error", err)
		return
	}
	running := map[string]bool{}
	a.mu.Lock()
	containers := make([]*container, 0, len(a.containers))
	for _, c := range a.containers {
		containers = append(containers, c)
	}
	a.mu.Unlock()
	for _, c := range containers {
		c.mu.Lock()
		live := c.started && c.phase != hostproto.ContainerPhase_CONTAINER_PHASE_EXITED
		c.mu.Unlock()
		if live {
			for _, d := range c.heldDisks() {
				running[d.ID] = true
			}
		}
	}
	for _, d := range disks {
		if d.Attached && !running[d.DiskID] {
			if err := a.diskEngine.Recover(ctx, d.DiskID); err != nil {
				a.log.Warn("recovering a disk failed", "disk_id", d.DiskID, "error", err)
			}
		}
	}
}

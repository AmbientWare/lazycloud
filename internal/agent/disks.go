package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/moby/moby/api/types/mount"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/diskengine"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// Durable disks: the agent leases each disk a container declares, restores
// it with the disk engine, bind-mounts it and publishes a generation every
// publishEvery. Leases are recorded in a host-wide lease directory before
// attaching; once the container has exited, an agent-owned loop publishes
// the last generation, detaches and releases each lease, retrying until the
// server accepts, across agent restarts. Until the release the server
// reports the disk as saving and no other container can take it.
const (
	publishEvery = 2 * time.Minute
	// compactAfter is how many committed layers the engine folds together.
	compactAfter = 8
	// acquireWait bounds how long a start waits for another holder to
	// release the disk.
	acquireWait = 5 * time.Minute
	// diskMinFree is the space restores leave free on the host.
	diskMinFree = 20 << 30
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

// diskSet is a container's leased disks.
type diskSet struct {
	mu    sync.Mutex
	disks []*heldDisk
}

func (a *Agent) leaseDir() string { return filepath.Join(a.cfg.StateDir, "disks", "leases") }

func (a *Agent) leaseFile(container string) string {
	return filepath.Join(a.leaseDir(), container+".json")
}

func (a *Agent) diskMount(container, disk string) string {
	return filepath.Join(a.cfg.StateDir, "disks", "mounts", container, disk)
}

// diskLock serializes publishing of one disk between a container's publish
// loop and the release loop.
func (a *Agent) diskLock(disk string) *sync.Mutex {
	lock, _ := a.diskLocks.LoadOrStore(disk, &sync.Mutex{})
	return lock.(*sync.Mutex) //nolint:forcetypeassert // The map holds only mutexes.
}

func (a *Agent) diskStore(workspace string) (diskengine.Store, error) {
	loc, err := a.volumes.location(workspace)
	if err != nil {
		return diskengine.Store{}, err
	}
	return diskengine.Store{
		Endpoint: loc.Endpoint, Region: loc.Region, Bucket: loc.Bucket, ForcePathStyle: loc.Endpoint != "",
		Credentials: func(context.Context) (diskengine.Credentials, error) {
			c, err := a.volumes.credentials(workspace)
			if err != nil {
				return diskengine.Credentials{}, err
			}
			return diskengine.Credentials{AccessKeyID: c.AccessKeyID, SecretAccessKey: c.SecretAccessKey, SessionToken: c.SessionToken, ExpiresAt: c.expires}, nil
		},
	}, nil
}

// attachDisks leases and attaches the container's disks and returns their
// bind mounts. Each lease is recorded before attaching, so a failure part
// way is released by the release loop like any other exit.
//
// A devbox's disk at / is its root filesystem: it is mounted at devboxRoot,
// where the supervisor seeds it and switches into it.
func (c *container) attachDisks(ctx context.Context, specs []*hostproto.DiskAttachment, devbox bool) ([]mount.Mount, []string, error) {
	if len(specs) == 0 {
		return nil, nil, nil
	}
	if c.a.diskErr != nil {
		return nil, nil, fmt.Errorf("this host cannot attach disks: %w", c.a.diskErr)
	}
	var binds []mount.Mount
	var workspaces []string
	for _, spec := range specs {
		target := spec.GetMountPath()
		switch {
		case target == "/" && devbox:
			target = devboxRoot
		case target == "/" || !filepath.IsAbs(target):
			return nil, nil, fmt.Errorf("disk %s: Docker hosts mount disks at an absolute directory, not %q", spec.GetName(), spec.GetMountPath())
		}
		ctx, span := telemetry.Start(ctx, "agent.disk_attach", trace.WithAttributes(attribute.String("lazycloud.disk", spec.GetName())))
		lease, err := c.acquire(ctx, spec.GetName())
		if err != nil {
			telemetry.Fail(span, err)
			return nil, nil, err
		}
		held := &heldDisk{ID: lease.GetDiskId(), Name: spec.GetName(), Workspace: lease.GetWorkspaceId(), Token: lease.GetLeaseToken()}
		c.disks.mu.Lock()
		c.disks.disks = append(c.disks.disks, held)
		err = c.a.saveLeases(c.id, c.disks.disks)
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
		telemetry.Fail(span, err)
		if err != nil {
			return nil, nil, fmt.Errorf("attach disk %s: %w", spec.GetName(), err)
		}
		binds = append(binds, mount.Mount{Type: mount.TypeBind, Source: request.Mountpoint, Target: target})
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

// saveLeases records a container's leases, or removes the record when it
// holds none.
func (a *Agent) saveLeases(container string, disks []*heldDisk) error {
	if len(disks) == 0 {
		if err := os.Remove(a.leaseFile(container)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("remove disk leases: %w", err)
		}
		return nil
	}
	data, err := json.Marshal(disks)
	if err != nil {
		return fmt.Errorf("encode disk leases: %w", err)
	}
	if err := os.MkdirAll(a.leaseDir(), 0o700); err != nil {
		return fmt.Errorf("create lease directory: %w", err)
	}
	if err := writeFileAtomic(a.leaseFile(container), data, 0o600); err != nil {
		return fmt.Errorf("record disk leases: %w", err)
	}
	return nil
}

func (a *Agent) loadLeases(container string) ([]*heldDisk, error) {
	data, err := os.ReadFile(a.leaseFile(container))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read disk leases: %w", err)
	}
	var disks []*heldDisk
	if err := json.Unmarshal(data, &disks); err != nil {
		return nil, fmt.Errorf("decode disk leases of %s: %w", container, err)
	}
	return disks, nil
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
		c.disks.mu.Lock()
		disks := append([]*heldDisk(nil), c.disks.disks...)
		c.disks.mu.Unlock()
		for _, d := range disks {
			if err := c.a.publish(ctx, c.id, d); err != nil && ctx.Err() == nil {
				c.log.Warn("publishing disk failed; retrying next round", "disk", d.Name, "error", err)
			}
		}
	}
}

// publish uploads every unpublished layer of d and records each with the
// server before committing it locally, so a lost reply is replayed.
func (a *Agent) publish(ctx context.Context, container string, d *heldDisk) error {
	lock := a.diskLock(d.ID)
	lock.Lock()
	defer lock.Unlock()
	store, err := a.diskStore(d.Workspace)
	if err != nil {
		return err
	}
	for {
		published, err := a.diskEngine.Publish(ctx, d.ID, store)
		if err != nil {
			return fmt.Errorf("publish: %w", err)
		}
		if published == nil {
			return nil
		}
		if _, err := a.host.RecordDiskGeneration(ctx, &hostproto.RecordDiskGenerationRequest{
			ContainerId: container, DiskId: d.ID, LeaseToken: d.Token,
			Generation: published.Generation, ParentGeneration: published.ParentGeneration,
			ManifestKey: published.ManifestKey, ManifestSha256: published.ManifestSHA256,
			AddedBytes: published.AddedBytes, Flat: published.Flat,
		}); err != nil {
			return fmt.Errorf("record generation %d: %w", published.Generation, err)
		}
		if err := a.diskEngine.CommitPublished(d.ID, published.Generation); err != nil { //nolint:contextcheck // A local state write.
			return fmt.Errorf("commit generation %d: %w", published.Generation, err)
		}
		d.Layers++
		if published.Flat {
			chain := []diskengine.Generation{{Generation: published.Generation, ManifestKey: published.ManifestKey, ManifestSHA256: published.ManifestSHA256}}
			removed, err := a.diskEngine.Collect(ctx, d.ID, store, chain)
			if err != nil {
				return fmt.Errorf("collect: %w", err)
			}
			if _, err := a.host.RecordDiskCollection(ctx, &hostproto.RecordDiskCollectionRequest{
				ContainerId: container, DiskId: d.ID, LeaseToken: d.Token, RemovedBytes: removed, BaseGeneration: published.Generation,
			}); err != nil {
				return fmt.Errorf("record collection: %w", err)
			}
		}
		if d.Layers >= compactAfter {
			if err := a.diskEngine.Compact(ctx, d.ID); err != nil {
				return fmt.Errorf("compact: %w", err)
			}
			d.Layers = 0
		}
	}
}

// release publishes d's last generation, detaches it and ends its lease. A
// disk this host never restored has nothing to publish. A lease the server
// already ended, as after host loss, counts as released.
func (a *Agent) release(ctx context.Context, container string, d *heldDisk) (err error) {
	ctx, span := telemetry.StartIn(ctx, a.tracer(), "", "agent.disk_release", trace.WithAttributes(
		telemetry.Container(container), attribute.String("lazycloud.disk_id", d.ID)))
	defer func() { telemetry.Fail(span, err) }()
	local, err := a.hasLocalDisk(ctx, d.ID)
	if err != nil {
		return err
	}
	if local {
		err := a.publish(ctx, container, d)
		if errors.Is(err, diskengine.ErrNoLocalState) || status.Code(err) == codes.FailedPrecondition {
			err = nil
		}
		if err == nil {
			err = a.diskEngine.Detach(ctx, d.ID)
		}
		if err != nil {
			return err
		}
	}
	_, err = a.host.ReleaseDisk(ctx, &hostproto.ReleaseDiskRequest{ContainerId: container, DiskId: d.ID, LeaseToken: d.Token})
	if status.Code(err) == codes.FailedPrecondition {
		return nil
	}
	if err != nil {
		return fmt.Errorf("release: %w", err)
	}
	return nil
}

// hasLocalDisk reports whether the engine keeps any state of disk here; a
// failed attach may have left none.
func (a *Agent) hasLocalDisk(ctx context.Context, disk string) (bool, error) {
	disks, err := a.diskEngine.List(ctx)
	if err != nil {
		return false, fmt.Errorf("list local disks: %w", err)
	}
	for _, d := range disks {
		if d.DiskID == disk {
			return true, nil
		}
	}
	return false, nil
}

// releaseLeases releases the leases of every container that no longer runs
// here, keeping the record of any that fails for the next round.
func (a *Agent) releaseLeases(ctx context.Context) {
	entries, err := os.ReadDir(a.leaseDir())
	if errors.Is(err, os.ErrNotExist) {
		return
	}
	if err != nil {
		a.log.Warn("listing disk leases failed", "error", err)
		return
	}
	for _, entry := range entries {
		container, ok := strings.CutSuffix(entry.Name(), ".json")
		if !ok || !isUUID(container) {
			continue
		}
		if c := a.lookup(container); c != nil && !c.hasExited() {
			continue
		}
		disks, err := a.loadLeases(container)
		if err != nil {
			a.log.Error("reading disk leases failed", "container_id", container, "error", err)
			continue
		}
		var kept []*heldDisk
		for _, d := range disks {
			if err := a.release(ctx, container, d); err != nil {
				a.log.Warn("releasing a disk failed; retrying", "container_id", container, "disk", d.Name, "error", err)
				kept = append(kept, d)
			}
		}
		if err := a.saveLeases(container, kept); err != nil {
			a.log.Error("recording disk leases failed", "container_id", container, "error", err)
		}
	}
}

// releaseLoop retries lease releases every minute and when a container
// exits.
func (a *Agent) releaseLoop(ctx context.Context) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		a.releaseLeases(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-a.releaseNow:
		}
	}
}

// requestRelease wakes the release loop.
func (a *Agent) requestRelease() {
	select {
	case a.releaseNow <- struct{}{}:
	default:
	}
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
		if c.hasExited() {
			continue
		}
		c.disks.mu.Lock()
		for _, d := range c.disks.disks {
			running[d.ID] = true
		}
		c.disks.mu.Unlock()
	}
	for _, d := range disks {
		if d.Attached && !running[d.DiskID] {
			if err := a.diskEngine.Recover(ctx, d.DiskID); err != nil {
				a.log.Warn("recovering a disk failed", "disk_id", d.DiskID, "error", err)
			}
		}
	}
}

package diskengine

import (
	"context"
	"errors"
	"fmt"
	"os"
	"slices"
	"time"
)

// flattenDepth is the committed chain depth at which the next publish
// flattens the chain into one self-contained generation, bounding how many
// layers a restore downloads.
const flattenDepth = 64

// jobPoll is how often a block job's status is read.
const jobPoll = 200 * time.Millisecond

func requireLiveAttachment(p diskPaths, state *diskState) error {
	a := state.Attachment
	if a == nil || !a.Mounted {
		return fmt.Errorf("%w: disk %s", ErrNotAttached, p.id)
	}
	if !daemonAlive(p, a.DaemonPID) {
		return fmt.Errorf("qemu-storage-daemon %d for disk %s is not running; recover it", a.DaemonPID, p.id)
	}
	return nil
}

// Publish seals the head of an attached disk when it took writes, then
// uploads the oldest sealed layer not yet published. It returns nil when no
// sealed layer awaits publishing. The upload stays pending until
// CommitPublished; until then a retry returns the same upload. A detached
// disk, such as one Recover sealed, publishes without sealing. Once the
// committed chain is flattenDepth deep the upload flattens the chain into a
// parentless generation.
func (e *Engine) Publish(ctx context.Context, diskID string, store Store) (*Published, error) {
	p, err := e.paths(diskID)
	if err != nil {
		return nil, err
	}
	lock, err := lockDisk(ctx, p)
	if err != nil {
		return nil, err
	}
	defer lock.release()
	state, err := requireState(p)
	if err != nil {
		return nil, err
	}
	if state.Attachment != nil {
		if err := requireLiveAttachment(p, state); err != nil {
			return nil, err
		}
		if err := seal(ctx, p, state); err != nil {
			return nil, err
		}
	}
	depth, err := state.chainDepth()
	if err != nil {
		return nil, err
	}
	published, err := publishOldest(ctx, p, state, store, depth >= flattenDepth)
	if err != nil || published == nil {
		return published, err
	}
	e.log.InfoContext(ctx, "disk layer uploaded", "disk_id", p.id, "generation", published.Generation,
		"parent_generation", published.ParentGeneration, "added_bytes", published.AddedBytes, "flat", published.Flat)
	return published, nil
}

// seal freezes the mounted filesystem, so everything it wrote reaches the
// head and nothing more arrives, switches the daemon to a new head when the
// old one took writes, and thaws.
func seal(ctx context.Context, p diskPaths, state *diskState) error {
	client, err := dialQMP(ctx, p.qmpSocket())
	if err != nil {
		return err
	}
	err = func() error {
		if err := reconcileHead(ctx, p, state, client); err != nil {
			return err
		}
		mountpoint := state.Attachment.Mountpoint
		if err := freezeFilesystem(mountpoint); err != nil {
			return err
		}
		_, err := sealFrozen(ctx, p, state, client)
		return errors.Join(err, thawFilesystem(mountpoint))
	}()
	return errors.Join(err, client.close())
}

// sealFrozen runs while nothing writes to the head. It reports whether it
// sealed the head under a new one.
func sealFrozen(ctx context.Context, p diskPaths, state *diskState, client *qmpClient) (bool, error) {
	head := state.head()
	written, err := headWritten(ctx, client, head.node())
	if err != nil {
		return false, err
	}
	if !written && state.HeadFresh {
		return false, nil
	}
	next := state.newLayer()
	path := p.layerPath(next)
	if err := createOverlay(ctx, path, head, state.SizeBytes); err != nil {
		return false, err
	}
	// Recorded before the switch; reconcileHead drops it again if the switch
	// never happened.
	state.Layers = append(state.Layers, next)
	wasFresh := state.HeadFresh
	state.HeadFresh = true
	if err := saveState(p, state); err != nil {
		return false, errors.Join(err, removeIfExists(path))
	}
	rollback := func(cause error) (bool, error) {
		state.Layers = state.Layers[:len(state.Layers)-1]
		state.HeadFresh = wasFresh
		return false, errors.Join(cause, saveState(p, state), removeIfExists(path))
	}
	err = client.execute(ctx, "blockdev-add", map[string]any{
		"driver":    string(formatQcow2),
		"node-name": next.node(),
		"discard":   "unmap",
		"file":      fileChild(path),
		"backing":   nil,
	}, nil)
	if err != nil {
		return rollback(err)
	}
	err = client.execute(ctx, "transaction", map[string]any{
		"actions": []any{map[string]any{
			"type": "blockdev-snapshot",
			"data": map[string]any{"node": head.node(), "overlay": next.node()},
		}},
	}, nil)
	if err != nil {
		delErr := client.execute(context.WithoutCancel(ctx), "blockdev-del", map[string]any{"node-name": next.node()}, nil)
		return rollback(errors.Join(err, delErr))
	}
	return true, nil
}

// publishOldest uploads the oldest sealed unpublished layer, or with flat the
// chain up to it as one parentless layer, and records the upload as pending.
func publishOldest(ctx context.Context, p diskPaths, state *diskState, store Store, flat bool) (*Published, error) {
	index := state.oldestUnpublished()
	if index < 0 {
		return nil, nil // Nothing awaits publishing.
	}
	target := state.Layers[index]
	if pending := state.Pending; pending != nil && pending.Seq == target.Seq {
		return pending.published(), nil
	}
	below := int64(0)
	if index > 0 {
		below = state.Layers[index-1].Generation
	}
	generation := max(below, state.PublishedGeneration) + 1
	objects, err := openStore(store)
	if err != nil {
		return nil, err
	}
	var result publishResult
	if flat {
		result, err = uploadFlattened(ctx, objects, p, state.Layers[:index+1], generation)
	} else {
		result, err = uploadFile(ctx, objects, p.id, p.layerPath(target), generation, below)
	}
	if err != nil {
		return nil, err
	}
	state.Pending = &pendingPublish{Seq: target.Seq, Flat: flat, Result: result}
	if err := saveState(p, state); err != nil {
		return nil, err
	}
	return state.Pending.published(), nil
}

// CommitPublished marks generation, the pending upload, as published once
// the control plane has recorded it. Committing the newest committed
// generation again succeeds. It waits for the disk's other operations.
func (e *Engine) CommitPublished(diskID string, generation int64) error {
	p, err := e.paths(diskID)
	if err != nil {
		return err
	}
	lock, err := lockDisk(context.Background(), p)
	if err != nil {
		return err
	}
	defer lock.release()
	state, err := requireState(p)
	if err != nil {
		return err
	}
	if state.Pending == nil || state.Pending.Result.Generation != generation {
		if state.Pending == nil && state.PublishedGeneration == generation {
			return nil
		}
		return fmt.Errorf("%w: disk %s has no uploaded generation %d awaiting commit", ErrInvalid, p.id, generation)
	}
	if err := state.commitPending(); err != nil {
		return err
	}
	return saveState(p, state)
}

// Detach unmounts the disk, disconnects its device and stops its daemon. The
// local copy stays for the next attach, holding any writes no Publish
// sealed, so a release publishes before detaching. Detaching a detached disk
// succeeds.
func (e *Engine) Detach(ctx context.Context, diskID string) error {
	p, err := e.paths(diskID)
	if err != nil {
		return err
	}
	lock, err := lockDisk(ctx, p)
	if err != nil {
		return err
	}
	defer lock.release()
	state, err := loadState(p)
	if err != nil || state == nil {
		return err
	}
	return teardown(ctx, p, state)
}

// Compact commits the published layers above the base of an attached disk
// into the base and deletes them, so the chain stops growing with each
// publish. The head and unpublished layers are untouched.
func (e *Engine) Compact(ctx context.Context, diskID string) error {
	p, err := e.paths(diskID)
	if err != nil {
		return err
	}
	lock, err := lockDisk(ctx, p)
	if err != nil {
		return err
	}
	defer lock.release()
	state, err := requireState(p)
	if err != nil {
		return err
	}
	if err := requireLiveAttachment(p, state); err != nil {
		return err
	}
	compacted, err := compactLayers(ctx, p, state)
	if err != nil || compacted == 0 {
		return err
	}
	e.log.InfoContext(ctx, "disk compacted", "disk_id", p.id, "layers", compacted)
	return nil
}

// compactLayers commits the run of published layers above a published base
// into it through the running daemon and returns how many it removed.
func compactLayers(ctx context.Context, p diskPaths, state *diskState) (int, error) {
	// Published layers form a prefix of the chain; the head is never committed.
	top := 0
	for i := 1; i < len(state.Layers)-1 && state.Layers[0].Generation > 0 && state.Layers[i].Generation > 0; i++ {
		top = i
	}
	if top == 0 {
		return 0, nil
	}
	client, err := dialQMP(ctx, p.qmpSocket())
	if err != nil {
		return 0, err
	}
	removed, err := func() ([]layer, error) {
		if err := reconcileHead(ctx, p, state, client); err != nil {
			return nil, err
		}
		base := state.Layers[0]
		jobID := fmt.Sprintf("compact-%d", state.Layers[top].Seq)
		err := client.execute(ctx, "block-commit", map[string]any{
			"job-id":       jobID,
			"device":       state.head().node(),
			"top-node":     state.Layers[top].node(),
			"base-node":    base.node(),
			"backing-file": base.file(),
			"auto-dismiss": false,
		}, nil)
		if err != nil {
			return nil, err
		}
		if err := awaitJob(ctx, client, jobID); err != nil {
			return nil, err
		}
		removed := slices.Clone(state.Layers[1 : top+1])
		state.Layers[0].Generation = state.Layers[top].Generation
		state.Layers = slices.Delete(state.Layers, 1, top+1)
		if err := saveState(p, state); err != nil {
			return nil, err
		}
		return removed, releaseNodes(ctx, client, removed)
	}()
	if err = errors.Join(err, client.close()); err != nil {
		return 0, err
	}
	for _, l := range removed {
		if err := removeIfExists(p.layerPath(l)); err != nil {
			return 0, err
		}
	}
	return len(removed), nil
}

type namedNode struct {
	NodeName string `json:"node-name"`
}

// releaseNodes deletes the committed layers' nodes the daemon still holds,
// newest first. The monitor holds the daemon's top node at startup and every
// head a seal added, and keeps them and the older layers they back open after
// the commit drops them from the chain. Without this a compaction frees no
// space until the disk is detached.
func releaseNodes(ctx context.Context, client *qmpClient, layers []layer) error {
	for _, l := range slices.Backward(layers) {
		var nodes []namedNode
		if err := client.execute(ctx, "query-named-block-nodes", map[string]any{"flat": true}, &nodes); err != nil {
			return err
		}
		open := slices.ContainsFunc(nodes, func(node namedNode) bool { return node.NodeName == l.node() })
		if !open {
			continue
		}
		if err := client.execute(ctx, "blockdev-del", map[string]any{"node-name": l.node()}, nil); err != nil {
			return fmt.Errorf("release committed layer %s: %w", l.file(), err)
		}
	}
	return nil
}

type blockJob struct {
	ID     string `json:"id"`
	Status string `json:"status"`
	Error  string `json:"error"`
}

func awaitJob(ctx context.Context, client *qmpClient, id string) error {
	for {
		var jobs []blockJob
		if err := client.execute(ctx, "query-jobs", nil, &jobs); err != nil {
			return err
		}
		index := slices.IndexFunc(jobs, func(job blockJob) bool { return job.ID == id })
		if index < 0 {
			return fmt.Errorf("block job %s disappeared before concluding", id)
		}
		if job := jobs[index]; job.Status == "concluded" {
			if err := client.execute(ctx, "job-dismiss", map[string]any{"id": id}, nil); err != nil {
				return err
			}
			if job.Error != "" {
				return fmt.Errorf("block job %s failed: %s", id, job.Error)
			}
			return nil
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("block job %s still running: %w", id, ctx.Err())
		case <-time.After(jobPoll):
		}
	}
}

// Recover releases whatever an attachment from a previous agent still holds,
// unmounting, disconnecting and stopping its daemon, and seals a head that
// may hold writes nobody sealed so the next Publish uploads them. The writes
// are crash-consistent: whatever had reached the file. Call it for every
// disk List reports when the agent starts. A disk with no local state, or
// detached with a head known to be empty, needs nothing.
func (e *Engine) Recover(ctx context.Context, diskID string) error {
	p, err := e.paths(diskID)
	if err != nil {
		return err
	}
	lock, err := lockDisk(ctx, p)
	if err != nil {
		return err
	}
	defer lock.release()
	state, err := loadState(p)
	if err != nil || state == nil || (state.Attachment == nil && state.HeadFresh) {
		return err
	}
	if state.Attachment != nil {
		// The agent that attached this disk is gone, so the head's write
		// statistics may have gone with its daemon; the next seal must not
		// trust them.
		state.HeadFresh = false
		if err := teardown(ctx, p, state); err != nil {
			return err
		}
	}
	if err := sealOrphanedHead(ctx, p, state); err != nil {
		return err
	}
	e.log.InfoContext(ctx, "disk recovered", "disk_id", p.id, "unpublished_layers", state.unpublishedSealed())
	return nil
}

// sealOrphanedHead seals, with no daemon running, a head that holds writes
// nobody sealed.
func sealOrphanedHead(ctx context.Context, p diskPaths, state *diskState) error {
	head := state.head()
	held, err := layerHoldsData(ctx, p.layerPath(head), head.format())
	if err != nil {
		return err
	}
	if held {
		next := state.newLayer()
		if err := createOverlay(ctx, p.layerPath(next), head, state.SizeBytes); err != nil {
			return err
		}
		state.Layers = append(state.Layers, next)
	}
	state.HeadFresh = true
	return saveState(p, state)
}

// layerHoldsData reports whether the top layer of an image maps any range of
// its own, data or zeroes, instead of passing it through to its backing file.
func layerHoldsData(ctx context.Context, path string, format layerFormat) (bool, error) {
	extents, err := mapImage(ctx, path, format, false)
	if err != nil {
		return false, err
	}
	return slices.ContainsFunc(extents, func(extent mappedExtent) bool { return extent.Depth == 0 && extent.Present }), nil
}

// Evict deletes a detached disk's local copy. Writes no committed generation
// holds are lost; List reports which disks have them. It waits for the
// disk's other operations.
func (e *Engine) Evict(diskID string) error {
	p, err := e.paths(diskID)
	if err != nil {
		return err
	}
	lock, err := lockDisk(context.Background(), p)
	if err != nil {
		return err
	}
	defer lock.release()
	state, err := loadState(p)
	if err != nil {
		return err
	}
	if state != nil && state.Attachment != nil {
		return fmt.Errorf("%w: disk %s is attached at %s; detach it first", ErrInvalid, p.id, state.Attachment.Mountpoint)
	}
	if err := os.RemoveAll(p.dir()); err != nil {
		return fmt.Errorf("evict disk %s: %w", p.id, err)
	}
	e.log.Info("disk evicted", "disk_id", p.id)
	return nil
}

// List describes every disk kept under the root, for recovering them at
// startup and choosing what to evict. A directory without state is a restore
// that never finished and holds nothing unpublished.
func (e *Engine) List(ctx context.Context) ([]LocalDisk, error) {
	entries, err := os.ReadDir(e.root)
	if errors.Is(err, os.ErrNotExist) {
		return []LocalDisk{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("list %s: %w", e.root, err)
	}
	disks := []LocalDisk{}
	for _, entry := range entries {
		if !entry.IsDir() || !diskIDPattern.MatchString(entry.Name()) {
			continue
		}
		disk, err := describeDisk(ctx, diskPaths{root: e.root, id: entry.Name()})
		if err != nil {
			return nil, fmt.Errorf("describe disk %s: %w", entry.Name(), err)
		}
		disks = append(disks, disk)
	}
	return disks, nil
}

func describeDisk(ctx context.Context, p diskPaths) (LocalDisk, error) {
	disk := LocalDisk{DiskID: p.id}
	local, err := allocatedBytes(p.dir())
	if err != nil {
		return disk, err
	}
	disk.LocalBytes = local
	state, err := loadState(p)
	if err != nil {
		return disk, err
	}
	if state == nil {
		info, err := os.Stat(p.dir())
		if err != nil {
			return disk, fmt.Errorf("stat %s: %w", p.dir(), err)
		}
		disk.LastUsedAt = info.ModTime().UTC()
		return disk, nil
	}
	disk.Attached = state.Attachment != nil
	disk.LastUsedAt = state.LastUsedAt
	dirty, err := headDirty(ctx, p, state)
	if err != nil {
		return disk, err
	}
	disk.Unpublished = state.unpublishedSealed() > 0 || dirty
	return disk, nil
}

// headDirty reports whether the head may hold writes no seal has taken. A
// head the engine did not create empty might; a fresh one has writes only if
// its running daemon saw them. The daemon's monitor serves one client at a
// time, so a disk another call is working on is reported dirty rather than
// waited for.
func headDirty(ctx context.Context, p diskPaths, state *diskState) (bool, error) {
	if !state.HeadFresh {
		return true, nil
	}
	if state.Attachment == nil {
		return false, nil
	}
	lock, err := tryLockFile(p.lockPath())
	if err != nil || lock == nil {
		return true, err
	}
	defer lock.release()
	// Reload under the lock, because reading the daemon may correct the state.
	current, err := requireState(p)
	if err != nil {
		return false, err
	}
	if current.Attachment == nil || !current.HeadFresh {
		return !current.HeadFresh, nil
	}
	if !daemonAlive(p, current.Attachment.DaemonPID) {
		return true, nil
	}
	return daemonHeadWritten(ctx, p, current)
}

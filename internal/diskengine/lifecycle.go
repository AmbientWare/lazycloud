package diskengine

import (
	"context"
	"errors"
	"fmt"
	"os"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// Seal makes everything written to an attached disk so far part of a sealed
// layer, which Publish then uploads. It flushes the filesystem and freezes
// it only when the head took writes. An attachment whose daemon, device,
// mount or served base is gone is released and its head sealed with the
// writes that reached it; Seal then returns ErrAttachmentLost, saying what
// went, and the disk is detached. A detached disk whose head may hold
// unsealed writes is sealed without a daemon. Sealing a disk with nothing
// new succeeds without change.
func (e *Engine) Seal(ctx context.Context, diskID string) error {
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
	if state.Attachment == nil {
		if state.HeadFresh {
			return nil
		}
		return sealOrphanedHead(ctx, p, state)
	}
	if lost := attachmentLost(p, state); lost != nil {
		mountpoint := state.Attachment.Mountpoint
		if err := releaseAttachment(ctx, p, state); err != nil {
			return errors.Join(lost, err)
		}
		e.log.WarnContext(ctx, "disk attachment lost; sealed what reached it", "disk_id", p.id, "cause", lost)
		return fmt.Errorf("disk %s at %s: %w", p.id, mountpoint, lost)
	}
	return telemetry.Step(ctx, "diskengine.seal", func(ctx context.Context) error { return seal(ctx, p, state) })
}

// attachmentLost says what of a recorded attachment is gone, as an
// ErrAttachmentLost, or nil while all of it runs.
func attachmentLost(p diskPaths, state *diskState) error {
	a := state.Attachment
	if err := baseLost(a); err != nil {
		return fmt.Errorf("%w: %w", ErrAttachmentLost, err)
	}
	healthy, err := attachmentHealthy(p, state)
	switch {
	case err != nil:
		return fmt.Errorf("%w: %w", ErrAttachmentLost, err)
	case healthy:
		return nil
	}
	if !daemonAlive(p, a.DaemonPID) {
		return fmt.Errorf("%w: qemu-storage-daemon %d stopped", ErrAttachmentLost, a.DaemonPID)
	}
	return fmt.Errorf("%w: its NBD device %s or mount at %s went", ErrAttachmentLost, a.Device, a.Mountpoint)
}

// Status reports the bytes the disk's layers hold that no committed
// generation does, which the disk's dirty budget bounds, whether Stall
// stalls its writes, and an ErrAttachmentLost saying what went when an
// attachment stopped working. It takes no lock and changes nothing.
func (e *Engine) Status(diskID string) (dirty int64, stalled bool, err error) {
	p, err := e.paths(diskID)
	if err != nil {
		return 0, false, err
	}
	state, err := requireState(p)
	if err != nil {
		return 0, false, err
	}
	if state.Attachment != nil && state.Attachment.Mounted {
		if err := attachmentLost(p, state); err != nil {
			return 0, false, err
		}
	}
	for _, l := range state.Layers {
		n, err := allocatedBytes(p.layerPath(l))
		if err != nil {
			return 0, false, err
		}
		dirty += n
	}
	return dirty, state.Stalled, nil
}

// Stall freezes, or with stall false thaws, the filesystem of an attached
// disk, so its writers wait while it holds all the unpublished writes its
// budget allows. Seal and Publish work on a stalled disk; Detach thaws it.
func (e *Engine) Stall(ctx context.Context, diskID string, stall bool) error {
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
	if state.Stalled == stall || state.Attachment == nil || !state.Attachment.Mounted {
		return nil
	}
	// Recorded first: a thaw of a filesystem not frozen succeeds.
	state.Stalled = stall
	if err := saveState(p, state); err != nil {
		return err
	}
	if stall {
		return freezeFilesystem(state.Attachment.Mountpoint)
	}
	return thawIfFrozen(state.Attachment.Mountpoint)
}

// seal flushes the mounted filesystem and, when the head took writes, freezes
// it, so everything it wrote reaches the head and nothing more arrives,
// switches the daemon to a new head and thaws. Writes still in the page cache
// reach the device in the flush, so an idle disk is never frozen. A stalled
// disk is frozen already and stays so.
func seal(ctx context.Context, p diskPaths, state *diskState) error {
	client, err := dialQMP(ctx, p.qmpSocket())
	if err != nil {
		return err
	}
	err = func() error {
		if err := reconcileHead(ctx, p, state, client); err != nil {
			return err
		}
		if state.Stalled {
			_, err := sealFrozen(ctx, p, state, client)
			return err
		}
		mountpoint := state.Attachment.Mountpoint
		if err := syncFilesystem(mountpoint); err != nil {
			return err
		}
		written, err := headWritten(ctx, client, state.head().node())
		if err != nil {
			return err
		}
		if !written && state.HeadFresh {
			return nil
		}
		if err := freezeFilesystem(mountpoint); err != nil {
			return err
		}
		_, err = sealFrozen(ctx, p, state, client)
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
	if err := createLayer(ctx, path, state.SizeBytes); err != nil {
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
	if err := client.execute(ctx, "blockdev-add", layerBlockdev(p, next, nil, true), nil); err != nil {
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

// Detach unmounts the disk, disconnects its device, stops its daemon and
// has the snapshotter stop serving its generations and forget its grant,
// even for a disk whose attach failed before anything was kept. The local
// stack stays for the next attach, holding any writes no Publish sealed,
// so a release publishes before detaching. Detaching a detached disk
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
	if err != nil {
		return err
	}
	if state != nil {
		if err := teardown(ctx, p, state); err != nil {
			return err
		}
	}
	if err := e.bases.ReleaseDisk(ctx, p.id, 0); err != nil {
		return fmt.Errorf("release the served generations: %w", err)
	}
	return nil
}

// Recover releases whatever an attachment from a previous agent still holds,
// unmounting, disconnecting and stopping its daemon, and seals a head that
// may hold writes nobody sealed so the next Publish uploads them. The writes
// are crash-consistent: whatever had reached the file. Call it for every
// disk List reports attached whose container no longer runs when the agent
// starts. A disk with no local state, or detached with a head known to be
// empty, needs nothing.
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
		if err := releaseAttachment(ctx, p, state); err != nil {
			return err
		}
	} else if err := sealOrphanedHead(ctx, p, state); err != nil {
		return err
	}
	e.log.InfoContext(ctx, "disk recovered", "disk_id", p.id, "sealed_layers", len(state.sealed()))
	return nil
}

// releaseAttachment tears down an attachment whose holder is gone or broken
// and seals, with no daemon running, whatever reached its head. The daemon's
// write statistics may have gone with it, so the seal does not trust them.
func releaseAttachment(ctx context.Context, p diskPaths, state *diskState) error {
	state.HeadFresh = false
	if err := teardown(ctx, p, state); err != nil {
		return err
	}
	return sealOrphanedHead(ctx, p, state)
}

// sealOrphanedHead seals, with no daemon running, a head that holds writes
// nobody sealed.
func sealOrphanedHead(ctx context.Context, p diskPaths, state *diskState) error {
	held, err := layerHoldsData(ctx, p, state.head())
	if err != nil {
		return err
	}
	if held {
		next := state.newLayer()
		if err := createLayer(ctx, p.layerPath(next), state.SizeBytes); err != nil {
			return err
		}
		state.Layers = append(state.Layers, next)
	}
	state.HeadFresh = true
	return saveState(p, state)
}

// layerHoldsData reports whether a layer maps any range of its own, data or
// zeroes, instead of passing it through to the layer below.
func layerHoldsData(ctx context.Context, p diskPaths, l layer) (bool, error) {
	extents, err := mapLayers(ctx, p, []layer{l})
	if err != nil {
		return false, err
	}
	return len(extents) > 0, nil
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
// startup and choosing what to evict. A directory without state is an
// attach that never finished and holds nothing unpublished.
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
		if !entry.IsDir() || !imagefs.DiskID(entry.Name()) {
			continue
		}
		disk, err := describeDisk(diskPaths{root: e.root, id: entry.Name()})
		if err != nil {
			return nil, fmt.Errorf("describe disk %s: %w", entry.Name(), err)
		}
		disks = append(disks, disk)
	}
	return disks, nil
}

func describeDisk(p diskPaths) (LocalDisk, error) {
	state, err := loadState(p)
	if err != nil {
		return LocalDisk{}, err
	}
	return LocalDisk{DiskID: p.id, Attached: state != nil && state.Attachment != nil}, nil
}

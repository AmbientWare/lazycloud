package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"time"
)

type sealResult struct {
	Sealed bool `json:"sealed"`
	// Pending counts sealed layers not yet published, including any a failed
	// publish left behind, so a caller publishes them even when nothing new
	// was sealed.
	Pending int `json:"pending"`
}

func requireLiveAttachment(p diskPaths, state *diskState) error {
	a := state.Attachment
	if a == nil || !a.Mounted {
		return errNotAttached
	}
	if !daemonAlive(p, a.DaemonPID) {
		return fmt.Errorf("qemu-storage-daemon %d for disk %s is not running; run recover", a.DaemonPID, p.id)
	}
	return nil
}

func runSeal(ctx context.Context, args []string) (any, error) {
	f := newFlags("seal", true)
	if err := f.parse(args); err != nil {
		return nil, err
	}
	if err := requireTools(toolImage); err != nil {
		return nil, err
	}
	p := f.paths()
	lock, err := lockDisk(p)
	if err != nil {
		return nil, err
	}
	defer lock.release()
	state, err := requireState(p)
	if err != nil {
		return nil, err
	}
	if err := requireLiveAttachment(p, state); err != nil {
		return nil, err
	}
	client, err := dialQMP(ctx, p.qmpSocket())
	if err != nil {
		return nil, err
	}
	defer client.close()
	if err := reconcileHead(p, state, client); err != nil {
		return nil, err
	}

	mountpoint := state.Attachment.Mountpoint
	if err := freezeFilesystem(mountpoint); err != nil {
		return nil, err
	}
	sealed, err := sealFrozen(ctx, p, state, client)
	if thawErr := thawFilesystem(mountpoint); thawErr != nil {
		err = errors.Join(err, thawErr)
	}
	if err != nil {
		return nil, err
	}
	return sealResult{Sealed: sealed, Pending: state.unpublishedSealed()}, nil
}

// sealFrozen runs with the filesystem frozen, so everything it wrote has
// reached the head and nothing more arrives until the head has been replaced.
func sealFrozen(ctx context.Context, p diskPaths, state *diskState, client *qmpClient) (bool, error) {
	head := state.head()
	written, err := headWritten(client, head.node())
	if err != nil {
		return false, err
	}
	if !written && state.HeadFresh {
		return false, nil
	}
	next := state.newLayer()
	path := p.layerPath(next)
	if err := createOverlay(ctx, path, head.file(), state.SizeBytes); err != nil {
		return false, err
	}
	// Recorded before the switch; reconcileHead drops it again if the switch
	// never happened.
	state.Layers = append(state.Layers, next)
	wasFresh := state.HeadFresh
	state.HeadFresh = true
	if err := saveState(p, state); err != nil {
		os.Remove(path)
		return false, err
	}
	rollback := func(cause error) (bool, error) {
		state.Layers = state.Layers[:len(state.Layers)-1]
		state.HeadFresh = wasFresh
		return false, errors.Join(cause, saveState(p, state), os.Remove(path))
	}
	err = client.execute("blockdev-add", map[string]any{
		"driver":    "qcow2",
		"node-name": next.node(),
		"discard":   "unmap",
		"file":      fileChild(path),
		"backing":   nil,
	}, nil)
	if err != nil {
		return rollback(err)
	}
	err = client.execute("transaction", map[string]any{
		"actions": []any{map[string]any{
			"type": "blockdev-snapshot",
			"data": map[string]any{"node": head.node(), "overlay": next.node()},
		}},
	}, nil)
	if err != nil {
		delErr := client.execute("blockdev-del", map[string]any{"node-name": next.node()}, nil)
		return rollback(errors.Join(err, delErr))
	}
	return true, nil
}

// reconcileHead makes the recorded head match the node the daemon exports.
// They differ only when a seal stopped between recording a new head and
// switching to it.
func reconcileHead(p diskPaths, state *diskState, client *qmpClient) error {
	exported, err := exportedNode(client)
	if err != nil {
		return err
	}
	head := state.head()
	if exported == head.node() {
		return nil
	}
	if len(state.Layers) < 2 || exported != state.Layers[len(state.Layers)-2].node() {
		return fmt.Errorf("qemu-storage-daemon exports %s but disk %s records head %s", exported, p.id, head.node())
	}
	state.Layers = state.Layers[:len(state.Layers)-1]
	state.HeadFresh = false
	if err := saveState(p, state); err != nil {
		return err
	}
	if err := os.Remove(p.layerPath(head)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func runPublish(ctx context.Context, args []string) (any, error) {
	f := newFlags("publish", true)
	storePath := f.set.String("store", "", "STORE.json: workspace bucket credentials")
	generation := f.set.Int64("generation", 0, "generation this publish creates")
	parent := f.set.Int64("parent", 0, "generation the layer builds on")
	flatten := f.set.Bool("flatten", false, "publish the whole chain as one self-contained layer")
	f.require("store", "generation", "parent")
	if err := f.parse(args); err != nil {
		return nil, err
	}
	if *flatten {
		if err := requireTools(toolImage); err != nil {
			return nil, err
		}
	}
	p := f.paths()
	lock, err := lockDisk(p)
	if err != nil {
		return nil, err
	}
	defer lock.release()
	state, err := requireState(p)
	if err != nil {
		return nil, err
	}

	index := state.oldestUnpublished()
	if index < 0 {
		return nil, errors.New("no sealed layer awaits publishing")
	}
	below := int64(0)
	if index > 0 {
		below = state.Layers[index-1].Generation
	}
	if *parent != below {
		return nil, fmt.Errorf("the oldest unpublished layer builds on generation %d, not %d", below, *parent)
	}
	if *generation <= max(below, state.PublishedGeneration) {
		return nil, fmt.Errorf("--generation %d must be above the published generation %d", *generation, max(below, state.PublishedGeneration))
	}
	target := state.Layers[index]
	if pending := state.Pending; pending != nil && pending.Seq == target.Seq &&
		pending.Flat == *flatten && pending.Result.Generation == *generation {
		return pending.Result, nil
	}

	store, err := openStore(*storePath)
	if err != nil {
		return nil, err
	}
	source := p.layerPath(target)
	resultParent := *parent
	if *flatten {
		source = filepath.Join(p.dir(), fmt.Sprintf("flatten-%d.qcow2", *generation))
		defer os.Remove(source)
		if _, err := runTool(ctx, toolImage, "convert", "-O", "qcow2", p.layerPath(target), source); err != nil {
			return nil, err
		}
		resultParent = 0
	}
	result, err := uploadLayer(ctx, store, p.id, source, state.SizeBytes, *generation, resultParent)
	if err != nil {
		return nil, err
	}
	state.Pending = &pendingPublish{Seq: target.Seq, Flat: *flatten, Result: result}
	if err := saveState(p, state); err != nil {
		return nil, err
	}
	return result, nil
}

type generationResult struct {
	Generation int64 `json:"generation"`
}

func runCommitPublished(ctx context.Context, args []string) (any, error) {
	f := newFlags("commit-published", true)
	generation := f.set.Int64("generation", 0, "generation the control plane recorded")
	f.require("generation")
	if err := f.parse(args); err != nil {
		return nil, err
	}
	p := f.paths()
	lock, err := lockDisk(p)
	if err != nil {
		return nil, err
	}
	defer lock.release()
	state, err := requireState(p)
	if err != nil {
		return nil, err
	}
	if state.Pending == nil || state.Pending.Result.Generation != *generation {
		if state.Pending == nil && state.PublishedGeneration == *generation {
			return generationResult{Generation: *generation}, nil
		}
		return nil, fmt.Errorf("disk %s has no uploaded generation %d awaiting commit", p.id, *generation)
	}
	if err := commitPending(state); err != nil {
		return nil, err
	}
	if err := saveState(p, state); err != nil {
		return nil, err
	}
	return generationResult{Generation: *generation}, nil
}

func commitPending(state *diskState) error {
	pending := state.Pending
	for i := range state.Layers {
		if state.Layers[i].Seq == pending.Seq {
			state.Layers[i].Generation = pending.Result.Generation
			state.PublishedGeneration = pending.Result.Generation
			state.PublishedManifestSHA256 = pending.Result.ManifestSHA256
			state.Pending = nil
			return nil
		}
	}
	return fmt.Errorf("disk %s no longer holds the layer uploaded as generation %d", state.DiskID, pending.Result.Generation)
}

type detachResult struct {
	Detached bool `json:"detached"`
}

func runDetach(ctx context.Context, args []string) (any, error) {
	f := newFlags("detach", true)
	if err := f.parse(args); err != nil {
		return nil, err
	}
	p := f.paths()
	lock, err := lockDisk(p)
	if err != nil {
		return nil, err
	}
	defer lock.release()
	state, err := loadState(p)
	if err != nil {
		return nil, err
	}
	if state != nil {
		if err := teardown(ctx, p, state); err != nil {
			return nil, err
		}
	}
	return detachResult{Detached: true}, nil
}

type compactResult struct {
	CompactedLayers int `json:"compacted_layers"`
}

func runCompact(ctx context.Context, args []string) (any, error) {
	f := newFlags("compact", true)
	if err := f.parse(args); err != nil {
		return nil, err
	}
	p := f.paths()
	lock, err := lockDisk(p)
	if err != nil {
		return nil, err
	}
	defer lock.release()
	state, err := requireState(p)
	if err != nil {
		return nil, err
	}
	if err := requireLiveAttachment(p, state); err != nil {
		return nil, err
	}
	// Published layers form a prefix of the chain; the head is never committed.
	top := 0
	for i := 1; i < len(state.Layers)-1 && state.Layers[0].Generation > 0 && state.Layers[i].Generation > 0; i++ {
		top = i
	}
	if top == 0 {
		return compactResult{}, nil
	}
	client, err := dialQMP(ctx, p.qmpSocket())
	if err != nil {
		return nil, err
	}
	defer client.close()
	if err := reconcileHead(p, state, client); err != nil {
		return nil, err
	}

	base := state.Layers[0]
	jobID := fmt.Sprintf("compact-%d", state.Layers[top].Seq)
	err = client.execute("block-commit", map[string]any{
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
	for _, l := range removed {
		if err := os.Remove(p.layerPath(l)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
	}
	return compactResult{CompactedLayers: len(removed)}, nil
}

func awaitJob(ctx context.Context, client *qmpClient, id string) error {
	for {
		var jobs []struct {
			ID     string `json:"id"`
			Status string `json:"status"`
			Error  string `json:"error"`
		}
		if err := client.execute("query-jobs", nil, &jobs); err != nil {
			return err
		}
		found := false
		for _, job := range jobs {
			if job.ID != id {
				continue
			}
			found = true
			if job.Status == "concluded" {
				if err := client.execute("job-dismiss", map[string]any{"id": id}, nil); err != nil {
					return err
				}
				if job.Error != "" {
					return fmt.Errorf("block job %s failed: %s", id, job.Error)
				}
				return nil
			}
		}
		if !found {
			return fmt.Errorf("block job %s disappeared before concluding", id)
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("block job %s still running: %w", id, ctx.Err())
		case <-time.After(200 * time.Millisecond):
		}
	}
}

type recoverResult struct {
	Recovered []string `json:"recovered"`
}

func runRecover(ctx context.Context, args []string) (any, error) {
	f := newFlags("recover", false)
	if err := f.parse(args); err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(*f.root)
	if errors.Is(err, os.ErrNotExist) {
		return recoverResult{Recovered: []string{}}, nil
	}
	if err != nil {
		return nil, err
	}
	result := recoverResult{Recovered: []string{}}
	var failures []error
	for _, entry := range entries {
		if !entry.IsDir() || validateDiskID(entry.Name()) != nil {
			continue
		}
		recovered, err := recoverDisk(ctx, diskPaths{root: *f.root, id: entry.Name()})
		if err != nil {
			failures = append(failures, fmt.Errorf("disk %s: %w", entry.Name(), err))
			continue
		}
		if recovered {
			result.Recovered = append(result.Recovered, entry.Name())
		}
	}
	if len(failures) > 0 {
		return nil, errors.Join(failures...)
	}
	return result, nil
}

func recoverDisk(ctx context.Context, p diskPaths) (bool, error) {
	lock, err := lockDisk(p)
	if err != nil {
		return false, err
	}
	defer lock.release()
	state, err := loadState(p)
	if err != nil || state == nil || state.Attachment == nil {
		return false, err
	}
	// The worker that attached this disk is gone, so the head's write
	// statistics went with its daemon; the next seal must not trust them.
	state.HeadFresh = false
	if err := teardown(ctx, p, state); err != nil {
		return false, err
	}
	return true, nil
}

type evictResult struct {
	Evicted bool `json:"evicted"`
}

func runEvict(ctx context.Context, args []string) (any, error) {
	f := newFlags("evict", true)
	if err := f.parse(args); err != nil {
		return nil, err
	}
	p := f.paths()
	lock, err := lockDisk(p)
	if err != nil {
		return nil, err
	}
	defer lock.release()
	state, err := loadState(p)
	if err != nil {
		return nil, err
	}
	if state != nil && state.Attachment != nil {
		return nil, fmt.Errorf("disk %s is attached at %s; detach it first", p.id, state.Attachment.Mountpoint)
	}
	present, err := pathExists(p.dir())
	if err != nil {
		return nil, err
	}
	if err := os.RemoveAll(p.dir()); err != nil {
		return nil, err
	}
	return evictResult{Evicted: present}, nil
}

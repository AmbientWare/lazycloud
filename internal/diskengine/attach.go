package diskengine

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sync/errgroup"
)

// Attach restores the disk if its local copy is not current, starts its
// daemon, connects an NBD device and mounts it. Attaching a disk already
// healthy at the same mountpoint and size succeeds without change. A disk
// grows to SizeBytes but never shrinks.
func (e *Engine) Attach(ctx context.Context, req AttachRequest) (AttachResult, error) {
	p, err := e.paths(req.DiskID)
	if err != nil {
		return AttachResult{}, err
	}
	if req.SizeBytes <= 0 || req.SizeBytes%filesystemBlockBytes != 0 {
		return AttachResult{}, fmt.Errorf("%w: size must be a positive multiple of %d, got %d", ErrInvalid, filesystemBlockBytes, req.SizeBytes)
	}
	if req.MinFreeBytes < 0 {
		return AttachResult{}, fmt.Errorf("%w: minimum free bytes must not be negative, got %d", ErrInvalid, req.MinFreeBytes)
	}
	if !filepath.IsAbs(req.Mountpoint) {
		return AttachResult{}, fmt.Errorf("%w: mountpoint must be absolute, got %q", ErrInvalid, req.Mountpoint)
	}
	if err := validateChain(req.Chain); err != nil {
		return AttachResult{}, err
	}
	if err := p.checkSocketPaths(); err != nil {
		return AttachResult{}, err
	}
	store, err := openStore(req.Store)
	if err != nil {
		return AttachResult{}, err
	}
	if err := e.Check(); err != nil {
		return AttachResult{}, err
	}
	target := filepath.Clean(req.Mountpoint)

	lock, err := lockDisk(ctx, p)
	if err != nil {
		return AttachResult{}, err
	}
	defer lock.release()

	state, err := loadState(p)
	if err != nil {
		return AttachResult{}, err
	}
	if state != nil && state.Attachment != nil {
		healthy, err := attachmentHealthy(p, state)
		if err != nil {
			return AttachResult{}, err
		}
		if healthy {
			if state.Attachment.Mountpoint != target {
				return AttachResult{}, fmt.Errorf("%w: disk %s is already attached at %s", ErrInvalid, p.id, state.Attachment.Mountpoint)
			}
			if state.SizeBytes != req.SizeBytes {
				return AttachResult{}, fmt.Errorf("%w: disk %s is attached at %d bytes; detach it before attaching at %d",
					ErrInvalid, p.id, state.SizeBytes, req.SizeBytes)
			}
			return AttachResult{Generation: state.PublishedGeneration, Reused: true}, nil
		}
		// An agent that died left this attachment; its daemon or device is gone.
		if err := teardown(ctx, p, state); err != nil {
			return AttachResult{}, fmt.Errorf("release the previous attachment: %w", err)
		}
	}

	state, result, err := e.prepare(ctx, p, state, req, store)
	if err != nil {
		return AttachResult{}, err
	}
	if err := startAttachment(ctx, p, state, target); err != nil {
		return AttachResult{}, err
	}
	result.Formatted = state.Unformatted
	if err := connectAndMount(ctx, p, state); err != nil {
		return AttachResult{}, errors.Join(err, teardown(context.WithoutCancel(ctx), p, state))
	}
	e.log.InfoContext(ctx, "disk attached", "disk_id", p.id, "generation", result.Generation,
		"reused", result.Reused, "formatted", result.Formatted, "device", state.Attachment.Device)
	return result, nil
}

// prepare makes the local chain hold the newest generation in req.Chain,
// reusing the local copy when it already does, and leaves a fresh head on top.
func (e *Engine) prepare(ctx context.Context, p diskPaths, state *diskState, req AttachRequest, store *objectStore) (*diskState, AttachResult, error) {
	newest := Generation{}
	if len(req.Chain) > 0 {
		newest = req.Chain[len(req.Chain)-1]
	}
	if state != nil && state.Pending != nil && state.Pending.Result.Generation == newest.Generation &&
		state.Pending.Result.ManifestSHA256 == newest.ManifestSHA256 {
		// The control plane recorded the upload; the confirmation never arrived.
		if err := state.commitPending(); err != nil {
			return nil, AttachResult{}, err
		}
		if err := saveState(p, state); err != nil {
			return nil, AttachResult{}, err
		}
	}

	result := AttachResult{Generation: newest.Generation}
	reuse, err := reusable(p, state, newest)
	if err != nil {
		return nil, AttachResult{}, err
	}
	if reuse {
		if state.SizeBytes > req.SizeBytes {
			return nil, AttachResult{}, fmt.Errorf("%w: disk %s is %d bytes and cannot shrink to %d", ErrInvalid, p.id, state.SizeBytes, req.SizeBytes)
		}
		result.Reused = true
		if state.SizeBytes < req.SizeBytes {
			if err := growHead(ctx, p, state, req.SizeBytes); err != nil {
				return nil, AttachResult{}, err
			}
		}
		return state, result, nil
	}

	manifests, err := fetchChain(ctx, store, p.id, req.Chain, req.SizeBytes)
	if err != nil {
		return nil, AttachResult{}, err
	}
	// Reusing the local copy downloads nothing, so it needs no reserve. A
	// restore is planned before the stale local copy is removed and counts
	// that copy as free.
	have, err := freeBytes(p)
	if err != nil {
		return nil, AttachResult{}, err
	}
	plan, err := planRestore(p, have, req.MinFreeBytes, manifests)
	if err != nil {
		return nil, AttachResult{}, err
	}
	if err := os.RemoveAll(p.dir()); err != nil {
		return nil, AttachResult{}, fmt.Errorf("remove stale local copy: %w", err)
	}
	if err := os.MkdirAll(p.layerDir(), 0o700); err != nil {
		return nil, AttachResult{}, fmt.Errorf("create layer directory: %w", err)
	}
	state = &diskState{DiskID: p.id, SizeBytes: req.SizeBytes, Published: []publishedRecord{}}
	if len(req.Chain) == 0 {
		base := state.newLayer()
		if err := createBase(ctx, p.layerPath(base), req.SizeBytes); err != nil {
			return nil, AttachResult{}, err
		}
		state.Layers = []layer{base}
		state.Unformatted = true
	} else {
		started := time.Now()
		restored, err := restoreChain(ctx, p, state, store, req.Chain, manifests, plan)
		if err != nil {
			return nil, AttachResult{}, err
		}
		state.GrowFilesystem = manifests[len(manifests)-1].VirtualSizeBytes < req.SizeBytes
		e.log.InfoContext(ctx, "disk restored", "disk_id", p.id, "generation", newest.Generation,
			"layers", len(req.Chain), "bytes", restored, "duration", time.Since(started))
	}
	state.HeadFresh = true
	if err := saveState(p, state); err != nil {
		return nil, AttachResult{}, err
	}
	return state, result, nil
}

// startAttachment starts the daemon and records the attachment.
func startAttachment(ctx context.Context, p diskPaths, state *diskState, mountpoint string) error {
	pid, err := startDaemon(ctx, p, state)
	if err != nil {
		return err
	}
	state.Attachment = &attachment{Mountpoint: mountpoint, DaemonPID: pid}
	state.LastUsedAt = time.Now().UTC()
	if err := saveState(p, state); err != nil {
		return errors.Join(err, stopDaemon(context.WithoutCancel(ctx), p, pid))
	}
	return nil
}

func connectAndMount(ctx context.Context, p diskPaths, state *diskState) error {
	device, err := connectNBD(ctx, p, state.SizeBytes, func(device string) error {
		state.Attachment.Device = device
		return saveState(p, state)
	})
	if err != nil {
		return err
	}
	if state.Unformatted {
		if err := formatExt4(ctx, device); err != nil {
			return err
		}
		state.Unformatted = false
		if err := saveState(p, state); err != nil {
			return err
		}
	}
	if err := mountExt4(device, state.Attachment.Mountpoint); err != nil {
		return err
	}
	state.Attachment.Mounted = true
	if err := saveState(p, state); err != nil {
		return err
	}
	if !state.GrowFilesystem {
		return nil
	}
	if err := growExt4(ctx, device); err != nil {
		return err
	}
	state.GrowFilesystem = false
	return saveState(p, state)
}

// growHead raises the disk's size to size before the daemon opens it. Only
// the head changes. The sealed layers below keep their size, and reads past
// the end of a smaller backing layer return zeroes. connectAndMount grows the
// filesystem once it is mounted. The flag asking for that is saved first, so
// an attach that stops between the two still grows it next time.
func growHead(ctx context.Context, p diskPaths, state *diskState, size int64) error {
	if _, err := runTool(ctx, toolImage, "resize", "-q", "-f", string(formatQcow2),
		p.layerPath(state.head()), strconv.FormatInt(size, 10)); err != nil {
		return err
	}
	state.SizeBytes = size
	state.GrowFilesystem = true
	return saveState(p, state)
}

// reusable reports whether the local chain already holds the newest published
// generation, so attaching needs no download. Anything the local chain holds
// beyond it was written by the last holder and is kept.
func reusable(p diskPaths, state *diskState, newest Generation) (bool, error) {
	if state == nil || state.PublishedGeneration != newest.Generation ||
		state.PublishedManifestSHA256 != newest.ManifestSHA256 {
		return false, nil
	}
	for _, l := range state.Layers {
		present, err := pathExists(p.layerPath(l))
		if err != nil || !present {
			return false, err
		}
	}
	return true, nil
}

// fetchChain reads and checks every manifest in the chain before anything
// local is removed or written. A disk only grows, so each generation is at
// least as large as the one it builds on and no larger than size.
func fetchChain(ctx context.Context, store *objectStore, diskID string, chain []Generation, size int64) ([]layerManifest, error) {
	manifests := make([]layerManifest, len(chain))
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(transferConcurrency)
	for i, entry := range chain {
		group.Go(func() error {
			manifest, err := fetchManifest(groupCtx, store, entry)
			if err != nil {
				return err
			}
			manifests[i] = manifest
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return nil, fmt.Errorf("fetch chain manifests: %w", err)
	}
	for i, entry := range chain {
		manifest := manifests[i]
		wantParent := int64(0)
		if i > 0 {
			wantParent = chain[i-1].Generation
		}
		switch {
		case manifest.DiskID != diskID:
			return nil, fmt.Errorf("%s belongs to disk %s", entry.ManifestKey, manifest.DiskID)
		case manifest.Generation != entry.Generation:
			return nil, fmt.Errorf("%s holds generation %d, the chain says %d", entry.ManifestKey, manifest.Generation, entry.Generation)
		case manifest.ParentGeneration != wantParent:
			return nil, fmt.Errorf("generation %d builds on %d, the chain puts it on %d", entry.Generation, manifest.ParentGeneration, wantParent)
		case manifest.VirtualSizeBytes > size:
			return nil, fmt.Errorf("%w: generation %d is %d bytes and cannot shrink to %d", ErrInvalid, entry.Generation, manifest.VirtualSizeBytes, size)
		case i > 0 && manifest.VirtualSizeBytes < manifests[i-1].VirtualSizeBytes:
			return nil, fmt.Errorf("generation %d is %d bytes, smaller than the %d bytes of the generation it builds on",
				entry.Generation, manifest.VirtualSizeBytes, manifests[i-1].VirtualSizeBytes)
		case manifest.Filesystem != diskFilesystem:
			return nil, fmt.Errorf("generation %d holds %s, not %s", entry.Generation, manifest.Filesystem, diskFilesystem)
		}
	}
	return manifests, nil
}

func blockRounded(n int64) int64 {
	return (n + filesystemBlockBytes - 1) / filesystemBlockBytes * filesystemBlockBytes
}

// storedBytes is the space restoring a layer takes: its chunks, not its
// holes, each rounded up to the filesystem blocks it fills.
func storedBytes(manifest layerManifest) int64 {
	var total int64
	for _, chunk := range manifest.Chunks {
		total += blockRounded(chunk.Length)
	}
	return total
}

// qcow2MetadataBytes bounds the tables a qcow2 image of virtual size virt
// needs on top of its data. 8-byte L2 entries and 2-byte refcounts per 64 KiB
// cluster come to under 1/4096 of the size, plus fixed headers.
func qcow2MetadataBytes(virt int64) int64 { return virt/4096 + 1<<20 }

// planRestore decides, before anything local is removed, whether a restore
// fits and when it must commit the layers it holds into the base to make room
// for the next. Each download must leave reserve free. A commit copies a held
// layer into the base before deleting it, so the base grows by at most that
// layer, and never past its virtual size and metadata; the peak during a
// commit must fit, though it may dip into the reserve. The restore follows the
// plan, so a chain that passes here does not fail for space halfway.
func planRestore(p diskPaths, have, reserve int64, manifests []layerManifest) ([]bool, error) {
	commitBefore := make([]bool, len(manifests))
	if len(manifests) == 0 {
		if have < reserve {
			return nil, &InsufficientSpaceError{Root: p.root, Need: 0, Have: have, Reserve: reserve}
		}
		return commitBefore, nil
	}
	type held struct{ bytes, virt int64 }
	used := storedBytes(manifests[0])
	if have-used < reserve {
		return nil, &InsufficientSpaceError{Root: p.root, Need: used, Have: have, Reserve: reserve}
	}
	baseBytes, baseVirt := used, manifests[0].VirtualSizeBytes
	var holding []held
	for i := 1; i < len(manifests); i++ {
		need := storedBytes(manifests[i])
		if have-used-need < reserve && len(holding) > 0 {
			for _, layer := range holding {
				baseVirt = max(baseVirt, layer.virt)
				growth := max(min(layer.bytes, baseVirt+qcow2MetadataBytes(baseVirt)-baseBytes), 0)
				if used+growth > have {
					return nil, &InsufficientSpaceError{Root: p.root, Need: used + growth, Have: have}
				}
				baseBytes += growth
				used += growth - layer.bytes
			}
			holding = nil
			commitBefore[i] = true
		}
		if have-used-need < reserve {
			return nil, &InsufficientSpaceError{Root: p.root, Need: used + need, Have: have, Reserve: reserve}
		}
		used += need
		holding = append(holding, held{bytes: need, virt: manifests[i].VirtualSizeBytes})
	}
	return commitBefore, nil
}

// restoreChain downloads the chain, base first, committing the layers it
// holds into the base where the plan says the next would not fit. When the
// whole chain fits, every layer downloads at once and only the rebases, which
// name the layer below, run in order.
func restoreChain(ctx context.Context, p diskPaths, state *diskState, store *objectStore, chain []Generation, manifests []layerManifest, commitBefore []bool) (int64, error) {
	layers := make([]layer, len(chain))
	sizes := make([]int64, len(chain))
	prefetched := !slices.Contains(commitBefore, true)
	if prefetched {
		for i := range chain {
			layers[i] = state.newLayer()
			layers[i].Raw = manifests[i].Format == formatRaw
		}
		group, groupCtx := errgroup.WithContext(ctx)
		group.SetLimit(layerDownloadConcurrency)
		for i, entry := range chain {
			group.Go(func() error {
				bytes, err := downloadLayer(groupCtx, store, manifests[i], p.layerPath(layers[i]))
				if err != nil {
					return fmt.Errorf("restore generation %d: %w", entry.Generation, err)
				}
				sizes[i] = bytes
				return nil
			})
		}
		if err := group.Wait(); err != nil {
			return 0, fmt.Errorf("restore disk %s: %w", p.id, err)
		}
	}
	var restored int64
	for i, entry := range chain {
		manifest := manifests[i]
		next := layers[i]
		if !prefetched {
			if commitBefore[i] {
				if err := commitHeld(ctx, p, state); err != nil {
					return 0, err
				}
			}
			next = state.newLayer()
			next.Raw = manifest.Format == formatRaw
			bytes, err := downloadLayer(ctx, store, manifest, p.layerPath(next))
			if err != nil {
				return 0, fmt.Errorf("restore generation %d: %w", entry.Generation, err)
			}
			sizes[i] = bytes
		}
		restored += sizes[i]
		if err := stackRestored(ctx, p, state, next, entry, manifest); err != nil {
			return 0, err
		}
	}
	return restored, addRestoredHead(ctx, p, state, chain[len(chain)-1])
}

// stackRestored puts a restored layer on top of the chain as generation
// entry. Backing names are relative, so the chain survives the root moving.
func stackRestored(ctx context.Context, p diskPaths, state *diskState, l layer, entry Generation, manifest layerManifest) error {
	if len(state.Layers) > 0 {
		if err := rebaseOnto(ctx, p, l, state.head()); err != nil {
			return err
		}
	}
	l.Generation = entry.Generation
	state.Layers = append(state.Layers, l)
	state.Published = append(state.Published, publishedRecord{
		Generation:       entry.Generation,
		ParentGeneration: manifest.ParentGeneration,
		ManifestKey:      entry.ManifestKey,
		ManifestSHA256:   entry.ManifestSHA256,
	})
	return nil
}

func addRestoredHead(ctx context.Context, p diskPaths, state *diskState, newest Generation) error {
	head := state.newLayer()
	if err := createOverlay(ctx, p.layerPath(head), state.head(), state.SizeBytes); err != nil {
		return err
	}
	state.Layers = append(state.Layers, head)
	state.PublishedGeneration = newest.Generation
	state.PublishedManifestSHA256 = newest.ManifestSHA256
	return nil
}

func rebaseOnto(ctx context.Context, p diskPaths, l, below layer) error {
	_, err := runTool(ctx, toolImage, "rebase", "-u", "-F", string(below.format()), "-b", below.file(), p.layerPath(l))
	return err
}

// commitIntoBelow commits a restored layer into the layer below it. The layer
// below is opened the way the daemon opens the base, detecting zeroes and
// unmapping them, so ranges a later generation zeroed free their space there
// rather than being written out as zeroes. -d leaves the committed layer
// as it was instead of emptying it; the caller deletes it.
func commitIntoBelow(ctx context.Context, path string, below layer, belowPath string) error {
	escape := func(value string) string { return strings.ReplaceAll(value, ",", ",,") }
	opts := strings.Join([]string{
		"driver=" + string(formatQcow2),
		"file.driver=file",
		"file.filename=" + escape(path),
		"backing.driver=" + string(below.format()),
		"backing.file.driver=file",
		"backing.file.filename=" + escape(belowPath),
		"backing.discard=unmap",
		"backing.detect-zeroes=unmap",
	}, ",")
	_, err := runTool(ctx, toolImage, "commit", "-q", "-d", "--image-opts", opts)
	return err
}

// commitHeld commits the restored layers above the base into it, oldest
// first, deleting each once it is in.
func commitHeld(ctx context.Context, p diskPaths, state *diskState) error {
	base := state.Layers[0]
	for _, held := range state.Layers[1:] {
		if err := rebaseOnto(ctx, p, held, base); err != nil {
			return err
		}
		if err := commitIntoBelow(ctx, p.layerPath(held), base, p.layerPath(base)); err != nil {
			return err
		}
		if err := os.Remove(p.layerPath(held)); err != nil {
			return fmt.Errorf("remove committed layer: %w", err)
		}
		base.Generation = held.Generation
	}
	state.Layers = []layer{base}
	return nil
}

func attachmentHealthy(p diskPaths, state *diskState) (bool, error) {
	a := state.Attachment
	if !a.Mounted || a.Device == "" || !daemonAlive(p, a.DaemonPID) || !nbdConnected(a.Device) {
		return false, nil
	}
	points, err := mountsOf(a.Device)
	if err != nil {
		return false, err
	}
	return slices.Contains(points, a.Mountpoint), nil
}

// teardown unmounts, disconnects and stops whatever the attachment still
// holds, in that order, and records the disk as detached. Each step checks
// the live system rather than the record, so it also finishes a teardown
// that was interrupted or an agent that died mid-attach.
func teardown(ctx context.Context, p diskPaths, state *diskState) error {
	a := state.Attachment
	if a == nil {
		return nil
	}
	if a.Device != "" {
		points, err := mountsOf(a.Device)
		if err != nil {
			return err
		}
		if slices.Contains(points, a.Mountpoint) {
			if err := thawIfFrozen(a.Mountpoint); err != nil {
				return err
			}
			if err := unmount(a.Mountpoint); err != nil {
				return err
			}
			if points, err = mountsOf(a.Device); err != nil {
				return err
			}
		}
		if len(points) > 0 {
			return fmt.Errorf("device busy: %s is still mounted at %v", a.Device, points)
		}
		if err := disconnectNBD(ctx, a.Device); err != nil {
			return err
		}
	}
	if daemonAlive(p, a.DaemonPID) {
		if state.HeadFresh {
			written, err := daemonHeadWritten(ctx, p, state)
			if err != nil {
				return err
			}
			state.HeadFresh = !written
		}
		if err := stopDaemon(ctx, p, a.DaemonPID); err != nil {
			return err
		}
	} else {
		state.HeadFresh = false
	}
	state.Attachment = nil
	state.LastUsedAt = time.Now().UTC()
	return saveState(p, state)
}

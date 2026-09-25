package main

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"golang.org/x/sync/errgroup"
	"golang.org/x/sys/unix"
)

type attachResult struct {
	Mountpoint    string `json:"mountpoint"`
	Generation    int64  `json:"generation"`
	RestoredBytes int64  `json:"restored_bytes"`
	ReusedLocal   bool   `json:"reused_local"`
	// Lazy is true when the restore fetched no data up front: the published
	// layers fill in from the bucket as they are read.
	Lazy bool `json:"lazy"`
	// Serving is true while `serve` runs for the disk, which it does until
	// every lazy layer is complete.
	Serving bool `json:"serving"`
	// Warnings name what the attach skipped that only costs prefetch order.
	Warnings []string `json:"warnings"`
}

func runAttach(ctx context.Context, args []string) (any, error) {
	f := newFlags("attach", true)
	size := f.set.Int64("size", 0, "disk size in bytes")
	mountpoint := f.set.String("mountpoint", "", "where to mount the filesystem")
	chainPath := f.set.String("chain", "", "CHAIN.json: published generations to restore, base first")
	storePath := f.set.String("store", "", "STORE.json: workspace bucket credentials")
	minFree := f.set.Int64("min-free-bytes", 0, "free space the filesystem under --root must keep after a restore")
	f.require("size", "mountpoint", "chain", "store")
	if err := f.parse(args); err != nil {
		return nil, err
	}
	if *size <= 0 || *size%4096 != 0 {
		return nil, fmt.Errorf("--size must be a positive multiple of 4096, got %d", *size)
	}
	if *minFree < 0 {
		return nil, fmt.Errorf("--min-free-bytes must not be negative, got %d", *minFree)
	}
	if !filepath.IsAbs(*mountpoint) {
		return nil, fmt.Errorf("--mountpoint must be absolute, got %q", *mountpoint)
	}
	target := filepath.Clean(*mountpoint)
	if err := requireTools(toolDaemon, toolImage, toolNBDClient, toolMkfs, toolResizeFS); err != nil {
		return nil, err
	}
	if err := requireNBDModule(); err != nil {
		return nil, err
	}
	chain, err := readChain(*chainPath)
	if err != nil {
		return nil, err
	}
	store, err := openStore(*storePath)
	if err != nil {
		return nil, err
	}

	p := f.paths()
	if err := p.checkSocketPaths(); err != nil {
		return nil, err
	}
	lock, err := lockDisk(p)
	if err != nil {
		return nil, err
	}
	defer lock.release()

	state, err := loadState(p)
	if err != nil {
		return nil, err
	}
	if state != nil && state.Attachment == nil && state.ServerPID != 0 {
		if err := teardown(ctx, p, state); err != nil {
			return nil, err
		}
	}
	if state != nil && state.Attachment != nil {
		healthy, err := attachmentHealthy(p, state)
		if err != nil {
			return nil, err
		}
		if healthy {
			if state.Attachment.Mountpoint != target {
				return nil, fmt.Errorf("disk %s is already attached at %s", p.id, state.Attachment.Mountpoint)
			}
			if state.SizeBytes != *size {
				return nil, fmt.Errorf("disk %s is attached at %d bytes; detach it before attaching at %d", p.id, state.SizeBytes, *size)
			}
			return attachResult{Mountpoint: target, Generation: state.PublishedGeneration, ReusedLocal: true}, nil
		}
		// A worker that died left this attachment; its daemon or device is gone.
		if err := teardown(ctx, p, state); err != nil {
			return nil, fmt.Errorf("release the previous attachment: %w", err)
		}
	}

	newest := chainEntry{}
	if len(chain) > 0 {
		newest = chain[len(chain)-1]
	}
	if state != nil && state.Pending != nil && state.Pending.Result.Generation == newest.Generation &&
		state.Pending.Result.ManifestSHA256 == newest.ManifestSHA256 {
		// The control plane recorded the upload; the confirmation never arrived.
		if err := commitPending(state); err != nil {
			return nil, err
		}
	}

	result := attachResult{Mountpoint: target, Generation: newest.Generation}
	format := false
	reuse, err := reusable(p, state, newest)
	if err != nil {
		return nil, err
	}
	if reuse && state.SizeBytes > *size {
		return nil, fmt.Errorf("disk %s is %d bytes and cannot shrink to %d", p.id, state.SizeBytes, *size)
	}
	var manifests []layerManifest
	if !reuse {
		if manifests, err = fetchChain(ctx, store, p.id, chain, *size); err != nil {
			return nil, err
		}
	}
	// Reusing the local copy downloads nothing, so it needs no reserve. The
	// worker watches the room left for the head's writes. A restore is planned
	// before the stale local copy is removed and counts that copy as free. A
	// lazy restore fills every layer in eventually, so it needs room for all
	// of them; one that does not fit restores eagerly, committing as it goes.
	var plan []bool
	lazy := false
	if !reuse {
		have, err := freeBytes(p, true)
		if err != nil {
			return nil, err
		}
		if lazy, err = lazyFits(p, have, *minFree, manifests); err != nil {
			return nil, err
		}
		if !lazy {
			if plan, err = planRestore(p, have, *minFree, manifests); err != nil {
				return nil, err
			}
		}
	}
	if reuse {
		result.ReusedLocal = true
		if err := settleLazyLayers(p, state); err != nil {
			return nil, err
		}
		if state.SizeBytes < *size {
			if err := growHead(ctx, p, state, *size); err != nil {
				return nil, err
			}
		}
	} else {
		if err := os.RemoveAll(p.dir()); err != nil {
			return nil, err
		}
		if err := os.MkdirAll(p.layerDir(), 0o700); err != nil {
			return nil, err
		}
		state = &diskState{DiskID: p.id, SizeBytes: *size, Published: []publishedRecord{}}
		if len(chain) == 0 {
			base := state.newLayer()
			if err := createBase(ctx, p.layerPath(base), *size); err != nil {
				return nil, err
			}
			state.Layers = []layer{base}
			format = true
		} else if lazy {
			if result.Warnings, err = restoreLazy(ctx, p, state, store, chain, manifests); err != nil {
				return nil, err
			}
			result.Lazy = true
			state.GrowFilesystem = manifests[len(manifests)-1].VirtualSizeBytes < *size
		} else {
			restored, err := restoreChain(ctx, p, state, store, chain, manifests, plan)
			if err != nil {
				return nil, err
			}
			result.RestoredBytes = restored
			state.GrowFilesystem = manifests[len(manifests)-1].VirtualSizeBytes < *size
		}
		state.HeadFresh = true
		if err := saveState(p, state); err != nil {
			return nil, err
		}
	}

	if state.hasLazy() {
		if err := startServer(ctx, p, state, *storePath); err != nil {
			return nil, err
		}
	}
	pid, err := startDaemon(ctx, p, state)
	if err != nil {
		stopErr := stopServer(context.WithoutCancel(ctx), p, state)
		return nil, errors.Join(err, stopErr, saveState(p, state))
	}
	state.Attachment = &attachment{Mountpoint: target, DaemonPID: pid}
	state.LastUsedAt = time.Now().UTC()
	if err := saveState(p, state); err != nil {
		return nil, errors.Join(err, stopDaemon(context.WithoutCancel(ctx), p, pid))
	}
	if err := connectAndMount(ctx, p, state, format); err != nil {
		return nil, errors.Join(err, teardown(context.WithoutCancel(ctx), p, state))
	}
	result.Serving = state.hasLazy()
	return result, nil
}

func connectAndMount(ctx context.Context, p diskPaths, state *diskState, format bool) error {
	device, err := connectNBD(ctx, p, state.SizeBytes, func(device string) error {
		state.Attachment.Device = device
		return saveState(p, state)
	})
	if err != nil {
		return err
	}
	if format {
		if err := formatExt4(ctx, device); err != nil {
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
	if _, err := runTool(ctx, toolImage, "resize", "-q", "-f", "qcow2", p.layerPath(state.head()), fmt.Sprint(size)); err != nil {
		return err
	}
	state.SizeBytes = size
	state.GrowFilesystem = true
	return saveState(p, state)
}

// reusable reports whether the local chain already holds the newest published
// generation, so attaching needs no download. Anything the local chain holds
// beyond it was written by the last holder and is kept.
func reusable(p diskPaths, state *diskState, newest chainEntry) (bool, error) {
	if state == nil || state.PublishedGeneration != newest.Generation ||
		state.PublishedManifestSHA256 != newest.ManifestSHA256 {
		return false, nil
	}
	for _, l := range state.Layers {
		paths := []string{p.layerPath(l)}
		if l.Lazy {
			paths = append(paths, manifestPath(p, l), bitmapPath(p, l))
		}
		for _, path := range paths {
			present, err := pathExists(path)
			if err != nil {
				return false, err
			}
			if !present {
				return false, nil
			}
		}
	}
	return true, nil
}

// fetchChain reads and checks every manifest in the chain before anything
// local is removed or written. A disk only grows, so each generation is at
// least as large as the one it builds on and no larger than size.
func fetchChain(ctx context.Context, store *objectStore, diskID string, chain []chainEntry, size int64) ([]layerManifest, error) {
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
		return nil, err
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
			return nil, fmt.Errorf("generation %d is %d bytes and cannot shrink to %d", entry.Generation, manifest.VirtualSizeBytes, size)
		case i > 0 && manifest.VirtualSizeBytes < manifests[i-1].VirtualSizeBytes:
			return nil, fmt.Errorf("generation %d is %d bytes, smaller than the %d bytes of the generation it builds on", entry.Generation, manifest.VirtualSizeBytes, manifests[i-1].VirtualSizeBytes)
		case manifest.Filesystem != diskFilesystem:
			return nil, fmt.Errorf("generation %d holds %s, not %s", entry.Generation, manifest.Filesystem, diskFilesystem)
		}
	}
	return manifests, nil
}

type insufficientSpaceError struct {
	root                string
	need, have, reserve int64
}

func (e *insufficientSpaceError) Error() string {
	return fmt.Sprintf("insufficient space on %s: need %d, have %d free, reserve %d", e.root, e.need, e.have, e.reserve)
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

// freeBytes is the space the filesystem under root has for this disk. Space
// the restore frees by replacing this disk's stale local copy counts as free.
func freeBytes(p diskPaths, countStale bool) (int64, error) {
	var fs unix.Statfs_t
	if err := unix.Statfs(p.root, &fs); err != nil {
		return 0, fmt.Errorf("statfs %s: %w", p.root, err)
	}
	have := int64(fs.Bavail) * int64(fs.Bsize)
	if countStale {
		stale, err := allocatedBytes(p.dir())
		if err != nil {
			return 0, err
		}
		have += stale
	}
	return have, nil
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
			return nil, &insufficientSpaceError{root: p.root, need: 0, have: have, reserve: reserve}
		}
		return commitBefore, nil
	}
	type held struct{ bytes, virt int64 }
	used := storedBytes(manifests[0])
	if have-used < reserve {
		return nil, &insufficientSpaceError{root: p.root, need: used, have: have, reserve: reserve}
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
					return nil, &insufficientSpaceError{root: p.root, need: used + growth, have: have}
				}
				baseBytes += growth
				used += growth - layer.bytes
			}
			holding = nil
			commitBefore[i] = true
		}
		if have-used-need < reserve {
			return nil, &insufficientSpaceError{root: p.root, need: used + need, have: have, reserve: reserve}
		}
		used += need
		holding = append(holding, held{bytes: need, virt: manifests[i].VirtualSizeBytes})
	}
	return commitBefore, nil
}

// allocatedBytes is the disk space the files under dir occupy, holes excluded.
func allocatedBytes(dir string) (int64, error) {
	var total int64
	err := filepath.WalkDir(dir, func(path string, entry fs.DirEntry, err error) error {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		var stat unix.Stat_t
		if err := unix.Lstat(path, &stat); err != nil {
			if errors.Is(err, unix.ENOENT) {
				return nil
			}
			return err
		}
		total += stat.Blocks * 512
		return nil
	})
	return total, err
}

// restoreChain downloads the chain, base first, committing the layers it
// holds into the base where the plan says the next would not fit. When the
// whole chain fits, every layer downloads at once and only the rebases, which
// name the layer below, run in order.
func restoreChain(ctx context.Context, p diskPaths, state *diskState, store *objectStore, chain []chainEntry, manifests []layerManifest, commitBefore []bool) (int64, error) {
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
			return 0, err
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
func stackRestored(ctx context.Context, p diskPaths, state *diskState, l layer, entry chainEntry, manifest layerManifest) error {
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

func addRestoredHead(ctx context.Context, p diskPaths, state *diskState, newest chainEntry) error {
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
	_, err := runTool(ctx, toolImage, "rebase", "-u", "-F", below.format(), "-b", below.file(), p.layerPath(l))
	return err
}

func attachmentHealthy(p diskPaths, state *diskState) (bool, error) {
	a := state.Attachment
	if !a.Mounted || a.Device == "" || !daemonAlive(p, a.DaemonPID) || !nbdConnected(a.Device) ||
		(state.hasLazy() && !serverAlive(p, state.ServerPID)) {
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
// that was interrupted or a worker that died mid-attach.
func teardown(ctx context.Context, p diskPaths, state *diskState) error {
	a := state.Attachment
	if a == nil {
		// An attach that stopped between starting `serve` and the daemon.
		if state.ServerPID == 0 {
			return nil
		}
		return errors.Join(stopServer(ctx, p, state), saveState(p, state))
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
		if err := releaseClaim(a.Device, p); err != nil {
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
	// After the daemon, which reads the lazy layers through it.
	if err := stopServer(ctx, p, state); err != nil {
		return err
	}
	state.Attachment = nil
	state.LastUsedAt = time.Now().UTC()
	return saveState(p, state)
}

func daemonHeadWritten(ctx context.Context, p diskPaths, state *diskState) (bool, error) {
	client, err := dialQMP(ctx, p.qmpSocket())
	if err != nil {
		return false, err
	}
	defer client.close()
	if err := reconcileHead(p, state, client); err != nil {
		return false, err
	}
	return headWritten(client, state.head().node())
}

// commitIntoBelow commits a restored layer into the layer below it. The layer
// below is opened the way the daemon opens the base, detecting zeroes and
// unmapping them, so ranges a later generation zeroed free their space there
// rather than being written out as zeroes. -d deletes the layer afterwards
// instead of emptying it.
func commitIntoBelow(ctx context.Context, path string, below layer, belowPath string) error {
	escape := func(value string) string { return strings.ReplaceAll(value, ",", ",,") }
	opts := strings.Join([]string{
		"driver=" + formatQcow2,
		"file.driver=file",
		"file.filename=" + escape(path),
		"backing.driver=" + below.format(),
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
			return err
		}
		base.Generation = held.Generation
	}
	state.Layers = []layer{base}
	return nil
}

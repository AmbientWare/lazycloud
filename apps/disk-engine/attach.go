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

	"golang.org/x/sys/unix"
)

type attachResult struct {
	Mountpoint    string `json:"mountpoint"`
	Generation    int64  `json:"generation"`
	RestoredBytes int64  `json:"restored_bytes"`
	ReusedLocal   bool   `json:"reused_local"`
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
	if err := checkSpace(p, reuse, manifests, *minFree); err != nil {
		return nil, err
	}
	if reuse {
		result.ReusedLocal = true
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
		} else {
			restored, err := restoreChain(ctx, p, state, store, chain, manifests, *minFree)
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

	pid, err := startDaemon(ctx, p, state)
	if err != nil {
		return nil, err
	}
	state.Attachment = &attachment{Mountpoint: target, DaemonPID: pid}
	state.LastUsedAt = time.Now().UTC()
	if err := saveState(p, state); err != nil {
		return nil, errors.Join(err, stopDaemon(context.WithoutCancel(ctx), p, pid))
	}
	if err := connectAndMount(ctx, p, state, format); err != nil {
		return nil, errors.Join(err, teardown(context.WithoutCancel(ctx), p, state))
	}
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
// the head changes: the sealed layers below keep their size, and reads past
// the end of a smaller backing layer return zeroes. The filesystem is grown
// once it is mounted, and the flag that asks for it is saved first, so an
// attach that stops between the two still grows it next time.
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
		present, err := pathExists(p.layerPath(l))
		if err != nil {
			return false, err
		}
		if !present {
			return false, nil
		}
	}
	return true, nil
}

// fetchChain reads and checks every manifest in the chain before anything
// local is removed or written. A disk only grows, so each generation is at
// least as large as the one it builds on and no larger than size.
func fetchChain(ctx context.Context, store *objectStore, diskID string, chain []chainEntry, size int64) ([]layerManifest, error) {
	manifests := make([]layerManifest, len(chain))
	for i, entry := range chain {
		manifest, err := fetchManifest(ctx, store, entry)
		if err != nil {
			return nil, err
		}
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
		manifests[i] = manifest
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

// storedBytes is what restoring a layer writes: its chunks, not its holes.
func storedBytes(manifest layerManifest) int64 {
	var total int64
	for _, chunk := range manifest.Chunks {
		total += chunk.Length
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

// checkSpace refuses a restore that would leave the filesystem under root
// with less than reserve free. A chain that does not fit whole still restores
// when its base and largest layer do, by committing layers as it goes.
func checkSpace(p diskPaths, reuse bool, manifests []layerManifest, reserve int64) error {
	var need, largest int64
	for i, manifest := range manifests {
		need += storedBytes(manifest)
		if i > 0 {
			largest = max(largest, storedBytes(manifest))
		}
	}
	have, err := freeBytes(p, !reuse)
	if err != nil {
		return err
	}
	if have-need >= reserve {
		return nil
	}
	if len(manifests) > 1 && have-storedBytes(manifests[0])-largest >= reserve {
		return nil
	}
	return &insufficientSpaceError{root: p.root, need: need, have: have, reserve: reserve}
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

// restoreChain downloads the chain, base first. When the next layer would
// leave less than reserve free, the layers already downloaded above the base
// are committed into it first, so only as many layers are held as fit.
func restoreChain(ctx context.Context, p diskPaths, state *diskState, store *objectStore, chain []chainEntry, manifests []layerManifest, reserve int64) (int64, error) {
	var restored int64
	for i, entry := range chain {
		manifest := manifests[i]
		if i > 0 {
			if err := makeRoom(ctx, p, state, storedBytes(manifest), reserve); err != nil {
				return 0, err
			}
		}
		next := state.newLayer()
		next.Raw = manifest.Format == formatRaw
		bytes, err := downloadLayer(ctx, store, manifest, p.layerPath(next))
		if err != nil {
			return 0, fmt.Errorf("restore generation %d: %w", entry.Generation, err)
		}
		restored += bytes
		next.Generation = entry.Generation
		if i > 0 {
			// Backing names are relative, so the chain survives the root moving.
			below := state.Layers[len(state.Layers)-1]
			if _, err := runTool(ctx, toolImage, "rebase", "-u", "-F", below.format(), "-b", below.file(), p.layerPath(next)); err != nil {
				return 0, err
			}
		}
		state.Layers = append(state.Layers, next)
		state.Published = append(state.Published, publishedRecord{
			Generation:       entry.Generation,
			ParentGeneration: manifest.ParentGeneration,
			ManifestKey:      entry.ManifestKey,
			ManifestSHA256:   entry.ManifestSHA256,
		})
	}
	top := state.Layers[len(state.Layers)-1]
	head := state.newLayer()
	if err := createOverlay(ctx, p.layerPath(head), top, state.SizeBytes); err != nil {
		return 0, err
	}
	state.Layers = append(state.Layers, head)
	newest := chain[len(chain)-1]
	state.PublishedGeneration = newest.Generation
	state.PublishedManifestSHA256 = newest.ManifestSHA256
	return restored, nil
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
// that was interrupted or a worker that died mid-attach.
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
// rather than being written out as zeroes. -d: the layer is deleted afterwards
// rather than emptied.
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

// makeRoom commits the restored layers above the base into it, oldest first,
// when downloading need more bytes would leave less than reserve free.
func makeRoom(ctx context.Context, p diskPaths, state *diskState, need, reserve int64) error {
	have, err := freeBytes(p, false)
	if err != nil {
		return err
	}
	if have-need >= reserve {
		return nil
	}
	base := state.Layers[0]
	for _, held := range state.Layers[1:] {
		if _, err := runTool(ctx, toolImage, "rebase", "-u", "-F", base.format(), "-b", base.file(), p.layerPath(held)); err != nil {
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
	if have, err = freeBytes(p, false); err != nil {
		return err
	}
	if have-need < reserve {
		return &insufficientSpaceError{root: p.root, need: need, have: have, reserve: reserve}
	}
	return nil
}

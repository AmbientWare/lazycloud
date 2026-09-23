package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
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
	f.require("size", "mountpoint", "chain", "store")
	if err := f.parse(args); err != nil {
		return nil, err
	}
	if *size <= 0 || *size%4096 != 0 {
		return nil, fmt.Errorf("--size must be a positive multiple of 4096, got %d", *size)
	}
	if !filepath.IsAbs(*mountpoint) {
		return nil, fmt.Errorf("--mountpoint must be absolute, got %q", *mountpoint)
	}
	target := filepath.Clean(*mountpoint)
	if err := requireTools(toolDaemon, toolImage, toolNBDClient, toolMkfs); err != nil {
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
	if reuse {
		if state.SizeBytes != *size {
			return nil, fmt.Errorf("disk %s is %d bytes locally and %d requested; resizing a disk is not supported", p.id, state.SizeBytes, *size)
		}
		result.ReusedLocal = true
	} else {
		if err := os.RemoveAll(p.dir()); err != nil {
			return nil, err
		}
		if err := os.MkdirAll(p.layerDir(), 0o700); err != nil {
			return nil, err
		}
		state = &diskState{DiskID: p.id, SizeBytes: *size}
		if len(chain) == 0 {
			base := state.newLayer()
			if err := createBase(ctx, p.layerPath(base), *size); err != nil {
				return nil, err
			}
			state.Layers = []layer{base}
			format = true
		} else {
			restored, err := restoreChain(ctx, p, state, store, chain)
			if err != nil {
				return nil, err
			}
			result.RestoredBytes = restored
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

func restoreChain(ctx context.Context, p diskPaths, state *diskState, store *objectStore, chain []chainEntry) (int64, error) {
	var restored int64
	for i, entry := range chain {
		manifest, err := fetchManifest(ctx, store, entry)
		if err != nil {
			return 0, err
		}
		wantParent := int64(0)
		if i > 0 {
			wantParent = chain[i-1].Generation
		}
		switch {
		case manifest.DiskID != p.id:
			return 0, fmt.Errorf("%s belongs to disk %s", entry.ManifestKey, manifest.DiskID)
		case manifest.Generation != entry.Generation:
			return 0, fmt.Errorf("%s holds generation %d, the chain says %d", entry.ManifestKey, manifest.Generation, entry.Generation)
		case manifest.ParentGeneration != wantParent:
			return 0, fmt.Errorf("generation %d builds on %d, the chain puts it on %d", entry.Generation, manifest.ParentGeneration, wantParent)
		case manifest.VirtualSizeBytes != state.SizeBytes:
			return 0, fmt.Errorf("generation %d is %d bytes, the disk is %d; resizing a disk is not supported", entry.Generation, manifest.VirtualSizeBytes, state.SizeBytes)
		case manifest.Filesystem != diskFilesystem:
			return 0, fmt.Errorf("generation %d holds %s, not %s", entry.Generation, manifest.Filesystem, diskFilesystem)
		}
		next := state.newLayer()
		bytes, err := downloadLayer(ctx, store, manifest, p.layerPath(next))
		if err != nil {
			return 0, fmt.Errorf("restore generation %d: %w", entry.Generation, err)
		}
		restored += bytes
		if i > 0 {
			// Backing names are relative, so the chain survives the root moving.
			below := state.Layers[len(state.Layers)-1]
			if _, err := runTool(ctx, toolImage, "rebase", "-u", "-F", "qcow2", "-b", below.file(), p.layerPath(next)); err != nil {
				return 0, err
			}
		}
		next.Generation = entry.Generation
		state.Layers = append(state.Layers, next)
	}
	top := state.Layers[len(state.Layers)-1]
	head := state.newLayer()
	if err := createOverlay(ctx, p.layerPath(head), top.file(), state.SizeBytes); err != nil {
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

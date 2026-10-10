package diskengine

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"time"

	"go.opentelemetry.io/otel/attribute"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// Attach has the snapshotter serve the disk's base generation, prefetching
// the frames the disk read at its last start and before its last stop,
// starts its daemon over the local stack, connects an NBD device and mounts
// it. Nothing is downloaded first. The local stack is kept when it is on the
// same base, with any writes the last holder here left in it, and is
// otherwise replaced by an empty head. Attaching a disk already healthy at
// the same mountpoint and size succeeds without change. A disk grows to
// SizeBytes but never shrinks.
func (e *Engine) Attach(ctx context.Context, req AttachRequest) (AttachResult, error) {
	p, err := e.paths(req.DiskID)
	if err != nil {
		return AttachResult{}, err
	}
	if req.SizeBytes <= 0 || req.SizeBytes%filesystemBlockBytes != 0 {
		return AttachResult{}, fmt.Errorf("%w: size must be a positive multiple of %d, got %d", ErrInvalid, filesystemBlockBytes, req.SizeBytes)
	}
	if !filepath.IsAbs(req.Mountpoint) {
		return AttachResult{}, fmt.Errorf("%w: mountpoint must be absolute, got %q", ErrInvalid, req.Mountpoint)
	}
	if b := req.Base; b != nil && (b.Generation <= 0 || b.ManifestKey == "" || !sha256Pattern(b.ManifestSHA256)) {
		return AttachResult{}, fmt.Errorf("%w: generation %d needs a positive number, an index key and a sha256 index digest", ErrInvalid, b.Generation)
	}
	if err := p.checkSocketPaths(); err != nil {
		return AttachResult{}, err
	}
	if err := Check(); err != nil {
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
		if attachmentLost(p, state) == nil {
			if state.Attachment.Mountpoint != target {
				return AttachResult{}, fmt.Errorf("%w: disk %s is already attached at %s", ErrInvalid, p.id, state.Attachment.Mountpoint)
			}
			if state.SizeBytes != req.SizeBytes {
				return AttachResult{}, fmt.Errorf("%w: disk %s is attached at %d bytes; detach it before attaching at %d",
					ErrInvalid, p.id, state.SizeBytes, req.SizeBytes)
			}
			return AttachResult{Generation: generationOf(state.Base), Reused: true}, nil
		}
		// An agent that died left this attachment; its daemon or device is gone.
		if err := teardown(ctx, p, state); err != nil {
			return AttachResult{}, fmt.Errorf("release the previous attachment: %w", err)
		}
	}
	if err := stopUnrecordedDaemon(ctx, p); err != nil {
		return AttachResult{}, err
	}

	state, result, err := e.start(ctx, p, state, req, target)
	if err != nil {
		return AttachResult{}, err
	}
	result.Formatted = state.Unformatted
	err = telemetry.Step(ctx, "diskengine.connect_and_mount", func(ctx context.Context) error { return connectAndMount(ctx, p, state) },
		attribute.Bool("lazycloud.format", state.Unformatted))
	if err != nil {
		return AttachResult{}, errors.Join(err, teardown(context.WithoutCancel(ctx), p, state))
	}
	e.log.InfoContext(ctx, "disk attached", "disk_id", p.id, "generation", result.Generation,
		"reused", result.Reused, "formatted", result.Formatted, "device", state.Attachment.Device)
	return result, nil
}

// start has the snapshotter serve req.Base, prefetching, prepares the local
// stack on it and starts the daemon serving both.
func (e *Engine) start(ctx context.Context, p diskPaths, state *diskState, req AttachRequest, target string) (*diskState, AttachResult, error) {
	basePath, baseSize := "", int64(0)
	if req.Base != nil {
		err := telemetry.Step(ctx, "diskengine.serve_base", func(ctx context.Context) error {
			path, err := e.bases.ServeDisk(ctx, &imagefsproto.ServeDiskRequest{
				DiskId: p.id, Generation: req.Base.Generation, IndexKey: req.Base.ManifestKey, IndexSha256: req.Base.ManifestSHA256, Prefetch: true,
			})
			if err != nil {
				return fmt.Errorf("serve the base generation: %w", err)
			}
			info, err := os.Stat(path)
			if err != nil {
				return fmt.Errorf("stat the base generation: %w", err)
			}
			basePath, baseSize = path, info.Size()
			return nil
		}, attribute.Int64("lazycloud.generation", req.Base.Generation))
		if err != nil {
			return nil, AttachResult{}, err
		}
	}
	state, result, err := prepare(ctx, p, state, req, baseSize)
	if err != nil {
		return nil, AttachResult{}, err
	}
	err = telemetry.Step(ctx, "diskengine.start_daemon", func(ctx context.Context) error { return startAttachment(ctx, p, state, target, basePath) })
	return state, result, err
}

// prepare makes the local stack sit on req.Base, keeping it when it already
// does, and otherwise replacing it with an empty head. A base of baseSize
// bytes under a larger disk leaves the filesystem to grow once mounted.
func prepare(ctx context.Context, p diskPaths, state *diskState, req AttachRequest, baseSize int64) (*diskState, AttachResult, error) {
	if state != nil && state.Pending != nil && req.Base != nil && state.Pending.Generation == req.Base.Generation &&
		state.Pending.ManifestSHA256 == req.Base.ManifestSHA256 {
		// The control plane recorded the upload; the confirmation never arrived.
		held, err := state.commitPending(p)
		if err != nil {
			return nil, AttachResult{}, err
		}
		if err := saveState(p, state); err != nil {
			return nil, AttachResult{}, err
		}
		for _, l := range held {
			if err := removeIfExists(p.layerPath(l)); err != nil {
				return nil, AttachResult{}, err
			}
		}
	}
	result := AttachResult{Generation: generationOf(req.Base)}
	reuse, err := reusable(p, state, req.Base)
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
	if baseSize > req.SizeBytes {
		return nil, AttachResult{}, fmt.Errorf("%w: generation %d of disk %s is %d bytes and cannot shrink to %d", ErrInvalid, result.Generation, p.id, baseSize, req.SizeBytes)
	}
	// Writes a stale local copy holds were fenced off: the disk moved on
	// without them.
	if err := os.RemoveAll(p.dir()); err != nil {
		return nil, AttachResult{}, fmt.Errorf("remove stale local copy: %w", err)
	}
	if err := os.MkdirAll(p.layerDir(), 0o700); err != nil {
		return nil, AttachResult{}, fmt.Errorf("create layer directory: %w", err)
	}
	state = &diskState{DiskID: p.id, SizeBytes: req.SizeBytes, Base: req.Base, HeadFresh: true,
		Unformatted: req.Base == nil, GrowFilesystem: req.Base != nil && baseSize < req.SizeBytes}
	head := state.newLayer()
	if err := createLayer(ctx, p.layerPath(head), req.SizeBytes); err != nil {
		return nil, AttachResult{}, err
	}
	state.Layers = []layer{head}
	if err := saveState(p, state); err != nil {
		return nil, AttachResult{}, err
	}
	return state, result, nil
}

// generationOf is g's number, or 0 for no generation.
func generationOf(g *Generation) int64 {
	if g == nil {
		return 0
	}
	return g.Generation
}

// reusable reports whether the local stack sits on base, so attaching keeps
// it. Anything it holds beyond base was written by the last holder here.
func reusable(p diskPaths, state *diskState, base *Generation) (bool, error) {
	if state == nil || (state.Base == nil) != (base == nil) ||
		state.Base != nil && (state.Base.Generation != base.Generation || state.Base.ManifestSHA256 != base.ManifestSHA256) {
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

// startAttachment starts the daemon over the stack and the base file at
// basePath, and records the attachment.
func startAttachment(ctx context.Context, p diskPaths, state *diskState, mountpoint, basePath string) error {
	a := &attachment{Mountpoint: mountpoint}
	if basePath != "" {
		var err error
		if a.BasePath, a.BaseDevice, err = served(basePath); err != nil {
			return err
		}
	}
	pid, err := startDaemon(ctx, p, state, basePath)
	if err != nil {
		return err
	}
	a.DaemonPID = pid
	state.Attachment = a
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
// the head changes. The layers and base below keep their size, and reads
// past the end of a smaller one return zeroes. connectAndMount grows the
// filesystem once it is mounted. The flag asking for that is saved first,
// so an attach that stops between the two still grows it next time.
func growHead(ctx context.Context, p diskPaths, state *diskState, size int64) error {
	if _, err := runTool(ctx, toolImage, "resize", "-q", "-f", "qcow2", p.layerPath(state.head()), strconv.FormatInt(size, 10)); err != nil {
		return err
	}
	state.SizeBytes = size
	state.GrowFilesystem = true
	return saveState(p, state)
}

// attachmentHealthy reports whether the daemon, device and mount of the
// attachment run.
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

// baseLost says why the attachment's base file is no longer the one its
// daemon opened, if it is not: the snapshotter that served it stopped.
func baseLost(a *attachment) error {
	if a.BasePath == "" {
		return nil
	}
	_, device, err := served(a.BasePath)
	if err != nil {
		return fmt.Errorf("the snapshotter stopped serving the disk's base: %w", err)
	}
	if device != a.BaseDevice {
		return errors.New("the snapshotter serving the disk's base restarted")
	}
	return nil
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

package diskengine

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"

	"go.opentelemetry.io/otel/attribute"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
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
// SizeBytes but never shrinks. The host must pass Check.
func (e *Engine) Attach(ctx context.Context, req AttachRequest) error {
	p, err := e.paths(req.DiskID)
	if err != nil {
		return err
	}
	if req.SizeBytes <= 0 || req.SizeBytes%hostproto.DiskBlockBytes != 0 {
		return fmt.Errorf("%w: size must be a positive multiple of %d, got %d", ErrInvalid, hostproto.DiskBlockBytes, req.SizeBytes)
	}
	if !filepath.IsAbs(req.Mountpoint) {
		return fmt.Errorf("%w: mountpoint must be absolute, got %q", ErrInvalid, req.Mountpoint)
	}
	if b := req.Base; b != nil && (b.Generation <= 0 || !sha256Pattern(b.IndexSHA256)) {
		return fmt.Errorf("%w: generation %d needs a positive number and a sha256 index digest", ErrInvalid, b.Generation)
	}
	if err := p.checkSocketPaths(); err != nil {
		return err
	}
	target := filepath.Clean(req.Mountpoint)

	lock, err := lockDisk(ctx, p)
	if err != nil {
		return err
	}
	defer lock.release()

	state, err := loadState(p)
	if err != nil {
		return err
	}
	if state != nil && state.Attachment != nil {
		if attachmentLost(p, state) == nil {
			if state.Attachment.Mountpoint != target {
				return fmt.Errorf("%w: disk %s is already attached at %s", ErrInvalid, p.id, state.Attachment.Mountpoint)
			}
			if state.SizeBytes != req.SizeBytes {
				return fmt.Errorf("%w: disk %s is attached at %d bytes; detach it before attaching at %d",
					ErrInvalid, p.id, state.SizeBytes, req.SizeBytes)
			}
			return nil
		}
		// An agent that died left this attachment; its daemon or device is gone.
		if err := teardown(ctx, p, state); err != nil {
			return fmt.Errorf("release the previous attachment: %w", err)
		}
	}
	if err := stopUnrecordedDaemon(ctx, p); err != nil {
		return err
	}

	state, err = e.start(ctx, p, state, req, target)
	if err != nil {
		return err
	}
	formats := state.Unformatted
	err = telemetry.Step(ctx, "diskengine.connect_and_mount", func(ctx context.Context) error { return connectAndMount(ctx, p, state) },
		attribute.Bool("lazycloud.format", formats))
	if err != nil {
		return errors.Join(err, teardown(context.WithoutCancel(ctx), p, state))
	}
	e.log.InfoContext(ctx, "disk attached", "disk_id", p.id, "generation", generationOf(state.Base),
		"formatted", formats, "device", state.Attachment.Device)
	return nil
}

// start makes the local stack sit on req.Base, has the snapshotter serve
// req.Base, prefetching, and starts the daemon serving both. A stack
// already on req.Base is kept; any other is replaced by an empty head.
func (e *Engine) start(ctx context.Context, p diskPaths, state *diskState, req AttachRequest, target string) (*diskState, error) {
	if state != nil && state.Pending != nil && req.Base != nil && state.Pending.generation() == *req.Base {
		// The control plane recorded the upload; the confirmation never arrived.
		held, err := state.commitPending(p)
		if err != nil {
			return nil, err
		}
		if err := saveState(p, state); err != nil {
			return nil, err
		}
		for _, l := range held {
			if err := removeIfExists(p.layerPath(l)); err != nil {
				return nil, err
			}
		}
	}
	reuse, err := reusable(p, state, req.Base)
	if err != nil {
		return nil, err
	}
	if !reuse {
		// Writes a stale local copy holds were fenced off: the disk moved on
		// without them.
		if err := os.RemoveAll(p.dir()); err != nil {
			return nil, fmt.Errorf("remove stale local copy: %w", err)
		}
		if err := os.MkdirAll(p.layerDir(), 0o700); err != nil {
			return nil, fmt.Errorf("create layer directory: %w", err)
		}
	}
	basePath, baseSize := "", int64(0)
	if req.Base != nil {
		err := telemetry.Step(ctx, "diskengine.serve_base", func(ctx context.Context) error {
			path, err := e.bases.ServeDisk(ctx, &imagefsproto.ServeDiskRequest{
				DiskId: p.id, Generation: req.Base.Generation, IndexSha256: req.Base.IndexSHA256, IndexPath: p.baseIndex(), Prefetch: true,
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
			return nil, err
		}
	}
	if reuse {
		err = growKept(ctx, p, state, req.SizeBytes)
	} else {
		state, err = newStack(ctx, p, req, baseSize)
	}
	if err != nil {
		return nil, err
	}
	return state, telemetry.Step(ctx, "diskengine.start_daemon", func(ctx context.Context) error { return startAttachment(ctx, p, state, target, basePath) })
}

// growKept grows a kept stack to size bytes.
func growKept(ctx context.Context, p diskPaths, state *diskState, size int64) error {
	if state.SizeBytes > size {
		return fmt.Errorf("%w: disk %s is %d bytes and cannot shrink to %d", ErrInvalid, p.id, state.SizeBytes, size)
	}
	if state.SizeBytes < size {
		return growHead(ctx, p, state, size)
	}
	return nil
}

// newStack records a stack of one empty head on req.Base. A base of
// baseSize bytes under a larger disk leaves the filesystem to grow once
// mounted.
func newStack(ctx context.Context, p diskPaths, req AttachRequest, baseSize int64) (*diskState, error) {
	if baseSize > req.SizeBytes {
		return nil, fmt.Errorf("%w: generation %d of disk %s is %d bytes and cannot shrink to %d", ErrInvalid, generationOf(req.Base), p.id, baseSize, req.SizeBytes)
	}
	state := &diskState{DiskID: p.id, SizeBytes: req.SizeBytes, Base: req.Base, HeadFresh: true,
		Unformatted: req.Base == nil, GrowFilesystem: req.Base != nil && baseSize < req.SizeBytes}
	head := state.newLayer()
	if err := createLayer(ctx, p.layerPath(head), req.SizeBytes); err != nil {
		return nil, err
	}
	state.Layers = []layer{head}
	return state, saveState(p, state)
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
	if state == nil || (state.Base == nil) != (base == nil) || state.Base != nil && *state.Base != *base {
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
	state.Attachment, state.Stalled = nil, false
	return saveState(p, state)
}

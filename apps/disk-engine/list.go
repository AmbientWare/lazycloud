package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"time"
)

type listedDisk struct {
	Disk        string    `json:"disk"`
	Attached    bool      `json:"attached"`
	LocalBytes  int64     `json:"local_bytes"`
	LastUsedAt  time.Time `json:"last_used_at"`
	Unpublished bool      `json:"unpublished"`
}

// runList describes every disk kept under root, for choosing what to evict.
// A directory without state is a restore that never finished and holds
// nothing unpublished.
func runList(ctx context.Context, args []string) (any, error) {
	f := newFlags("list", false)
	if err := f.parse(args); err != nil {
		return nil, err
	}
	disks := []listedDisk{}
	entries, err := os.ReadDir(*f.root)
	if errors.Is(err, os.ErrNotExist) {
		return disks, nil
	}
	if err != nil {
		return nil, err
	}
	for _, entry := range entries {
		if !entry.IsDir() || validateDiskID(entry.Name()) != nil {
			continue
		}
		disk, err := describeDisk(ctx, diskPaths{root: *f.root, id: entry.Name()})
		if err != nil {
			return nil, err
		}
		disks = append(disks, disk)
	}
	return disks, nil
}

func describeDisk(ctx context.Context, p diskPaths) (listedDisk, error) {
	disk := listedDisk{Disk: p.id}
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
			return disk, err
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
// time, so a disk another command is working on is reported dirty rather
// than waited for.
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

type usageResult struct {
	// UnmergedBytes is the space the layers above the one compaction commits
	// into occupy. A compaction needs room to copy each of them into it.
	UnmergedBytes int64 `json:"unmerged_bytes"`
	// Unreadable says why the disk failed a read, empty while it has not. A
	// lazy layer whose chunk could not be fetched, or whose server stopped,
	// has already given the workload an I/O error.
	Unreadable string `json:"unreadable"`
}

// runUsage reads the disk's state without its lock, because the worker asks
// every few seconds and must get an answer while a publish holds the lock.
// State is replaced atomically, so a read sees one whole version of it.
func runUsage(ctx context.Context, args []string) (any, error) {
	f := newFlags("usage", true)
	if err := f.parse(args); err != nil {
		return nil, err
	}
	p := f.paths()
	state, err := requireState(p)
	if err != nil {
		return nil, err
	}
	var result usageResult
	// Compaction commits into the lowest local layer, so that one is not
	// unmerged unless it is the head, which is never committed.
	unmerged := min(state.lowestLocal()+1, len(state.Layers)-1)
	for _, l := range state.Layers[unmerged:] {
		allocated, err := allocatedBytes(p.layerPath(l))
		if err != nil {
			return nil, err
		}
		result.UnmergedBytes += allocated
	}
	if state.Attachment != nil && state.hasLazy() {
		status, err := readServeStatus(p)
		if err != nil {
			return nil, err
		}
		switch {
		case status.FailedReads > 0:
			result.Unreadable = fmt.Sprintf("%d reads failed; the last: %s", status.FailedReads, status.LastError)
		case state.Attachment.Mounted && !serverAlive(p, state.ServerPID):
			result.Unreadable = fmt.Sprintf("the process serving its lazy layers exited; see %s", p.serveLog())
		}
	}
	return result, nil
}

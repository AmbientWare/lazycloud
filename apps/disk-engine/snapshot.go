package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"

	"golang.org/x/sys/unix"
)

// snapshotPoint is what a volume made from a snapshot of this disk restores:
// the chain as published when the snapshot was taken, and the volume it was
// taken on.
type snapshotPoint struct {
	ID     string    `json:"id"`
	Volume string    `json:"volume"`
	State  diskState `json:"state"`
}

type snapshotPointResult struct {
	Generation int64  `json:"generation"`
	Point      string `json:"point"`
}

// runSnapshotPoint prepares the volume under root for a snapshot of the
// disk's newest committed generation. It records the published layers in
// snapshot.json and flushes the whole filesystem, so everything a restore
// reads is on the device before the caller asks for the snapshot. The head
// keeps taking writes throughout; a restore never reads it.
func runSnapshotPoint(ctx context.Context, args []string) (any, error) {
	f := newFlags("snapshot-point", true)
	volume := f.set.String("volume", "", "provider volume the root lives on")
	f.require("volume")
	if err := f.parse(args); err != nil {
		return nil, err
	}
	if *volume == "" {
		return nil, errors.New("--volume must name the provider volume")
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
	if state.Pending != nil {
		return nil, fmt.Errorf("disk %s has generation %d uploaded and not committed", p.id, state.Pending.Result.Generation)
	}
	published := state.publishedPrefix()
	if published == 0 || state.PublishedGeneration == 0 {
		return nil, fmt.Errorf("disk %s has no published generation to snapshot", p.id)
	}
	top := state.Layers[published-1]
	if top.Generation != state.PublishedGeneration {
		return nil, fmt.Errorf("disk %s records generation %d as published, but its newest published layer holds %d", p.id, state.PublishedGeneration, top.Generation)
	}
	size, err := layerVirtualSize(p.layerPath(top), top)
	if err != nil {
		return nil, err
	}
	id, err := randomID()
	if err != nil {
		return nil, err
	}
	point := snapshotPoint{ID: id, Volume: *volume, State: diskState{
		DiskID:                  state.DiskID,
		SizeBytes:               size,
		Layers:                  slices.Clone(state.Layers[:published]),
		NextSeq:                 state.NextSeq,
		GrowFilesystem:          state.GrowFilesystem,
		PublishedGeneration:     state.PublishedGeneration,
		PublishedManifestSHA256: state.PublishedManifestSHA256,
		Published:               slices.Clone(state.Published),
		LastUsedAt:              state.LastUsedAt,
	}}
	data, err := json.MarshalIndent(point, "", "  ")
	if err != nil {
		return nil, err
	}
	if err := writeFileAtomic(p.pointPath(), data); err != nil {
		return nil, err
	}
	if err := syncFilesystem(p.root); err != nil {
		return nil, err
	}
	return snapshotPointResult{Generation: state.PublishedGeneration, Point: id}, nil
}

// adoptSnapshot turns the state a snapshot copied from another volume into
// this volume's own. A volume made from a snapshot holds the source's state
// file, attachment and head included; the attachment names a daemon and a
// device on the machine that took the snapshot, and the head holds writes no
// generation recorded. The published layers the point lists are kept, every
// other file goes, and a fresh head is created over them.
//
// It does nothing on the volume the point was taken on, and nothing once the
// point has been adopted, so a retried attach keeps what the first one built.
func adoptSnapshot(ctx context.Context, p diskPaths, state *diskState, volume string) (*diskState, bool, error) {
	var point snapshotPoint
	if err := readJSONFile(p.pointPath(), &point); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return state, false, nil
		}
		return nil, false, err
	}
	if point.Volume == volume || (state != nil && state.AdoptedPoint == point.ID) {
		return state, false, nil
	}
	adopted := point.State
	if adopted.DiskID != p.id || len(adopted.Layers) == 0 || adopted.PublishedGeneration == 0 {
		return nil, false, fmt.Errorf("%s does not describe a published chain of disk %s", p.pointPath(), p.id)
	}
	adopted.AdoptedPoint = point.ID
	if err := os.RemoveAll(p.runDir()); err != nil {
		return nil, false, err
	}
	keep := map[string]bool{}
	for _, l := range adopted.Layers {
		keep[l.file()] = true
	}
	entries, err := os.ReadDir(p.layerDir())
	if err != nil {
		return nil, false, err
	}
	for _, entry := range entries {
		if keep[entry.Name()] {
			continue
		}
		if err := os.Remove(filepath.Join(p.layerDir(), entry.Name())); err != nil {
			return nil, false, err
		}
	}
	for _, l := range adopted.Layers {
		present, err := pathExists(p.layerPath(l))
		if err != nil {
			return nil, false, err
		}
		if !present {
			return nil, false, fmt.Errorf("the snapshot of disk %s lists layer %s but holds no such file", p.id, l.file())
		}
	}
	head := adopted.newLayer()
	if err := createOverlay(ctx, p.layerPath(head), adopted.head(), adopted.SizeBytes); err != nil {
		return nil, false, err
	}
	adopted.Layers = append(adopted.Layers, head)
	adopted.HeadFresh = true
	adopted.Hydrating = true
	if err := saveState(p, &adopted); err != nil {
		return nil, false, err
	}
	return &adopted, true, nil
}

func syncFilesystem(path string) error {
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	defer unix.Close(fd)
	if err := unix.Syncfs(fd); err != nil {
		return fmt.Errorf("sync the filesystem under %s: %w", path, err)
	}
	return nil
}

func randomID() (string, error) {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

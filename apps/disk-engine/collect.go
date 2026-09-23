package main

import (
	"context"
	"fmt"
	"path"
	"strconv"
	"strings"
)

type collectResult struct {
	RemovedBytes     int64 `json:"removed_bytes"`
	RemovedChunks    int   `json:"removed_chunks"`
	RemovedManifests int   `json:"removed_manifests"`
}

// runCollect deletes what no restore can reach once generation G, which
// needs no parent, is the newest the control plane recorded: every older
// manifest, and every chunk that neither G onwards nor an upload still
// awaiting its commit refers to. removed_bytes counts chunk bytes only, the
// same bytes stored_bytes_added counts on the way in.
func runCollect(ctx context.Context, args []string) (any, error) {
	f := newFlags("collect", true)
	storePath := f.set.String("store", "", "STORE.json: workspace bucket credentials")
	generation := f.set.Int64("generation", 0, "newest committed generation, which must have no parent")
	f.require("store", "generation")
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
	if *generation != state.PublishedGeneration {
		return nil, fmt.Errorf("generation %d is not the newest committed generation %d", *generation, state.PublishedGeneration)
	}
	floor, known := state.record(*generation)
	if !known {
		return nil, fmt.Errorf("disk %s has no record of generation %d", p.id, *generation)
	}
	if floor.ParentGeneration != 0 {
		return nil, fmt.Errorf("generation %d builds on %d; only a parentless generation can be collected to", *generation, floor.ParentGeneration)
	}
	store, err := openStore(*storePath)
	if err != nil {
		return nil, err
	}

	var live []chainEntry
	for _, record := range state.Published {
		if record.Generation >= *generation {
			live = append(live, chainEntry{Generation: record.Generation, ManifestKey: record.ManifestKey, ManifestSHA256: record.ManifestSHA256})
		}
	}
	if pending := state.Pending; pending != nil {
		live = append(live, chainEntry{Generation: pending.Result.Generation, ManifestKey: pending.Result.ManifestKey, ManifestSHA256: pending.Result.ManifestSHA256})
	}
	referenced := map[string]bool{}
	for _, entry := range live {
		manifest, err := fetchManifest(ctx, store, entry)
		if err != nil {
			return nil, err
		}
		for _, chunk := range manifest.Chunks {
			referenced[chunk.SHA256] = true
		}
	}

	var result collectResult
	var manifests []string
	err = store.list(ctx, diskObjectPrefix+"/"+p.id+"/manifests/", func(object storedObject) error {
		name, ok := strings.CutSuffix(path.Base(object.Key), ".json")
		if !ok {
			return nil
		}
		older, err := strconv.ParseInt(name, 10, 64)
		if err == nil && older < *generation && object.Key == diskManifestKey(p.id, older) {
			manifests = append(manifests, object.Key)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	var chunks []string
	err = store.list(ctx, diskObjectPrefix+"/"+p.id+"/chunks/", func(object storedObject) error {
		sum := path.Base(object.Key)
		if !sha256Pattern.MatchString(sum) || object.Key != diskChunkKey(p.id, sum) || referenced[sum] {
			return nil
		}
		chunks = append(chunks, object.Key)
		result.RemovedBytes += object.Size
		return nil
	})
	if err != nil {
		return nil, err
	}
	// Delete manifests first. A manifest whose chunks are gone breaks a
	// restore, while a chunk nothing names waits for the next collect.
	if err := store.deleteKeys(ctx, manifests); err != nil {
		return nil, err
	}
	if err := store.deleteKeys(ctx, chunks); err != nil {
		return nil, err
	}
	result.RemovedManifests = len(manifests)
	result.RemovedChunks = len(chunks)

	kept := state.Published[:0]
	for _, record := range state.Published {
		if record.Generation >= *generation {
			kept = append(kept, record)
		}
	}
	state.Published = kept
	if err := saveState(p, state); err != nil {
		return nil, err
	}
	return result, nil
}

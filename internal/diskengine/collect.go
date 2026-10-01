package diskengine

import (
	"context"
	"fmt"
	"path"
	"strconv"
	"strings"
)

// Collect deletes the disk's objects no restore can reach once chain, the
// control plane's current chain, starts at a parentless generation: every
// manifest older than chain's base, and every chunk that neither chain, a
// generation this host committed above its base, nor a pending upload refers
// to. It returns the chunk bytes deleted, the same bytes Published.AddedBytes
// counts on the way in. Manifests go first: a manifest whose chunks are gone
// breaks a restore, while a chunk nothing names waits for the next collect.
func (e *Engine) Collect(ctx context.Context, diskID string, store Store, chain []Generation) (int64, error) {
	p, err := e.paths(diskID)
	if err != nil {
		return 0, err
	}
	if len(chain) == 0 {
		return 0, fmt.Errorf("%w: collect needs the current chain", ErrInvalid)
	}
	if err := validateChain(chain); err != nil {
		return 0, err
	}
	objects, err := openStore(store)
	if err != nil {
		return 0, err
	}
	lock, err := lockDisk(ctx, p)
	if err != nil {
		return 0, err
	}
	defer lock.release()
	state, err := requireState(p)
	if err != nil {
		return 0, err
	}
	if chain[0].Generation > state.PublishedGeneration {
		return 0, fmt.Errorf("%w: disk %s has committed generation %d, below the chain's base %d; commit it first",
			ErrInvalid, p.id, state.PublishedGeneration, chain[0].Generation)
	}
	// Checks every manifest and that the base has no parent.
	manifests, err := fetchChain(ctx, objects, p.id, chain, state.SizeBytes)
	if err != nil {
		return 0, err
	}
	floor := chain[0].Generation

	referenced := map[string]bool{}
	inChain := map[int64]bool{}
	for _, manifest := range manifests {
		inChain[manifest.Generation] = true
		for _, chunk := range manifest.Chunks {
			referenced[chunk.SHA256] = true
		}
	}
	var extra []Generation
	for _, record := range state.Published {
		if record.Generation >= floor && !inChain[record.Generation] {
			extra = append(extra, Generation{Generation: record.Generation, ManifestKey: record.ManifestKey, ManifestSHA256: record.ManifestSHA256})
		}
	}
	if pending := state.Pending; pending != nil {
		extra = append(extra, Generation{Generation: pending.Result.Generation, ManifestKey: pending.Result.ManifestKey, ManifestSHA256: pending.Result.ManifestSHA256})
	}
	for _, entry := range extra {
		manifest, err := fetchManifest(ctx, objects, entry)
		if err != nil {
			return 0, err
		}
		for _, chunk := range manifest.Chunks {
			referenced[chunk.SHA256] = true
		}
	}

	var stale []string
	err = objects.list(ctx, objects.diskPrefix(p.id)+"manifests/", func(object storedObject) {
		name, ok := strings.CutSuffix(path.Base(object.Key), ".json")
		if !ok {
			return
		}
		number, digest, _ := strings.Cut(name, "-")
		older, err := strconv.ParseInt(number, 10, 64)
		if err == nil && older < floor && object.Key == objects.manifestKey(p.id, older, digest) {
			stale = append(stale, object.Key)
		}
	})
	if err != nil {
		return 0, err
	}
	var chunks []string
	var removed int64
	err = objects.list(ctx, objects.diskPrefix(p.id)+"chunks/", func(object storedObject) {
		sum := path.Base(object.Key)
		if !sha256Pattern.MatchString(sum) || object.Key != objects.chunkKey(p.id, sum) || referenced[sum] {
			return
		}
		chunks = append(chunks, object.Key)
		removed += object.Size
	})
	if err != nil {
		return 0, err
	}
	if err := objects.deleteKeys(ctx, stale); err != nil {
		return 0, err
	}
	if err := objects.deleteKeys(ctx, chunks); err != nil {
		return 0, err
	}

	kept := state.Published[:0]
	for _, record := range state.Published {
		if record.Generation >= floor {
			kept = append(kept, record)
		}
	}
	state.Published = kept
	if err := saveState(p, state); err != nil {
		return 0, err
	}
	e.log.InfoContext(ctx, "disk collected", "disk_id", p.id, "base_generation", floor,
		"removed_bytes", removed, "removed_chunks", len(chunks), "removed_manifests", len(stale))
	return removed, nil
}

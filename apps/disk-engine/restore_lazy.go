package main

import (
	"context"
	"fmt"
	"os"
	"slices"

	"golang.org/x/sync/errgroup"
)

// lazyFits reports whether a lazy restore of the chain leaves reserve free
// once every layer, and every other lazy disk under the same root, has
// filled in. The other disks' manifests and bitmaps are their reservation: it
// survives restarts and shrinks as their chunks arrive.
func lazyFits(p diskPaths, have, reserve int64, manifests []layerManifest) (bool, error) {
	need, err := owedByOthers(p)
	if err != nil {
		return false, err
	}
	for _, manifest := range manifests {
		need += storedBytes(manifest)
	}
	return have-need >= reserve, nil
}

func owedByOthers(p diskPaths) (int64, error) {
	entries, err := os.ReadDir(p.root)
	if err != nil {
		return 0, err
	}
	var owed int64
	for _, entry := range entries {
		if !entry.IsDir() || entry.Name() == p.id || validateDiskID(entry.Name()) != nil {
			continue
		}
		other := diskPaths{root: p.root, id: entry.Name()}
		state, err := loadState(other)
		if err != nil {
			return 0, err
		}
		if state == nil {
			continue
		}
		for _, l := range state.Layers[:state.lowestLocal()] {
			bytes, _, err := lazyOwed(other, l)
			if err != nil {
				return 0, err
			}
			owed += bytes
		}
	}
	return owed, nil
}

// restoreLazy lays the chain out without its data: each published layer is a
// sparse file that `serve` fills from the bucket as it is read. Only the
// qcow2 tables qemu reads to open a layer are fetched here, because the
// rebase below opens every layer and writes its header. It returns what it
// could not do that only costs prefetch order.
func restoreLazy(ctx context.Context, p diskPaths, state *diskState, store *objectStore, chain []chainEntry, manifests []layerManifest) ([]string, error) {
	layers := make([]layer, len(chain))
	for i := range chain {
		layers[i] = state.newLayer()
		layers[i].Raw = manifests[i].Format == formatRaw
		layers[i].Lazy = true
		if err := createLazyLayer(p, layers[i], manifests[i]); err != nil {
			return nil, err
		}
	}
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(transferConcurrency)
	for _, l := range layers {
		if !l.Raw {
			group.Go(func() error { return fetchOpenTables(groupCtx, p, l, store) })
		}
	}
	if err := group.Wait(); err != nil {
		return nil, err
	}
	for i, entry := range chain {
		if err := stackRestored(ctx, p, state, layers[i], entry, manifests[i]); err != nil {
			return nil, err
		}
	}
	var warnings []string
	if err := fetchHeat(ctx, p, store); err != nil {
		warnings = append(warnings, fmt.Sprintf("restoring without prefetch hints: %v", err))
	}
	return warnings, addRestoredHead(ctx, p, state, chain[len(chain)-1])
}

// fetchHeat copies the disk's heat map from its bucket prefix. A map that does
// not decode is deleted there too, since only prefetch order depends on it and
// the next session writes a fresh one.
func fetchHeat(ctx context.Context, p diskPaths, store *objectStore) error {
	key := diskHeatKey(p.id)
	data, found, err := store.getIfExists(ctx, key, maxHeatMapBytes)
	if err != nil || !found {
		return err
	}
	if _, err := decodeHeat(data); err != nil {
		return fmt.Errorf("%s: %w (deleted: %v)", key, err, store.deleteKeys(ctx, []string{key}))
	}
	return writeFileAtomic(p.heatPath(), data)
}

// fetchOpenTables makes present the parts of a qcow2 layer that opening it
// reads: the header cluster, the L1 table and the refcount table.
func fetchOpenTables(ctx context.Context, p diskPaths, l layer, store chunkSource) error {
	lazy, err := openLazyLayer(ctx, p, l, store)
	if err != nil {
		return err
	}
	defer lazy.close()
	raw := make([]byte, qcow2HeaderBytes)
	if err := lazy.ReadAt(ctx, raw, 0); err != nil {
		return err
	}
	header, err := parseQcow2Header(l.file(), raw)
	if err != nil {
		return err
	}
	cluster := header.clusterBytes
	for _, span := range [][2]int64{{0, cluster}, {header.l1Offset, header.l1Entries * 8}, {header.refcountOffset, header.refcountClusters * cluster}} {
		if err := lazy.ensure(ctx, span[0], min(span[1], lazy.Size()-span[0])); err != nil {
			return err
		}
	}
	return nil
}

// settleLazyLayers turns the lazy layers into plain files once all of them
// are complete, so the daemon opens them directly and compaction may write
// them. They change together because the lazy layers stay the bottom of the
// chain.
func settleLazyLayers(p diskPaths, state *diskState) error {
	if !state.hasLazy() {
		return nil
	}
	settled := slices.Clone(state.Layers[:state.lowestLocal()])
	for _, l := range settled {
		if _, complete, err := lazyOwed(p, l); err != nil || !complete {
			return err
		}
	}
	for i := range settled {
		state.Layers[i].Lazy = false
	}
	if err := saveState(p, state); err != nil {
		return err
	}
	for _, l := range settled {
		for _, path := range []string{manifestPath(p, l), bitmapPath(p, l)} {
			if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
				return err
			}
		}
	}
	return nil
}

package main

import (
	"context"
	"encoding/binary"
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
	for i, entry := range chain {
		layers[i] = state.newLayer()
		layers[i].Raw = manifests[i].Format == formatRaw
		layers[i].Lazy = true
		layers[i].Generation = entry.Generation
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
		if i > 0 {
			below := layers[i-1]
			if _, err := runTool(ctx, toolImage, "rebase", "-u", "-F", below.format(), "-b", below.file(), p.layerPath(layers[i])); err != nil {
				return nil, err
			}
		}
		state.Layers = append(state.Layers, layers[i])
		state.Published = append(state.Published, publishedRecord{
			Generation:       entry.Generation,
			ParentGeneration: manifests[i].ParentGeneration,
			ManifestKey:      entry.ManifestKey,
			ManifestSHA256:   entry.ManifestSHA256,
		})
	}
	var warnings []string
	if err := fetchHeat(ctx, p, store); err != nil {
		warnings = append(warnings, fmt.Sprintf("restoring without prefetch hints: %v", err))
	}
	head := state.newLayer()
	if err := createOverlay(ctx, p.layerPath(head), state.head(), state.SizeBytes); err != nil {
		return nil, err
	}
	state.Layers = append(state.Layers, head)
	newest := chain[len(chain)-1]
	state.PublishedGeneration = newest.Generation
	state.PublishedManifestSHA256 = newest.ManifestSHA256
	return warnings, nil
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
	header := make([]byte, 64)
	if err := lazy.ReadAt(ctx, header, 0); err != nil {
		return err
	}
	if string(header[:4]) != "QFI\xfb" {
		return fmt.Errorf("layer %s is not a qcow2 image", l.file())
	}
	clusterBits := binary.BigEndian.Uint32(header[20:24])
	if clusterBits < 9 || clusterBits > 21 {
		return fmt.Errorf("layer %s has %d-bit clusters", l.file(), clusterBits)
	}
	cluster := int64(1) << clusterBits
	l1Entries := int64(binary.BigEndian.Uint32(header[36:40]))
	l1Offset := int64(binary.BigEndian.Uint64(header[40:48]))
	refcountOffset := int64(binary.BigEndian.Uint64(header[48:56]))
	refcountClusters := int64(binary.BigEndian.Uint32(header[56:60]))
	for _, span := range [][2]int64{{0, cluster}, {l1Offset, l1Entries * 8}, {refcountOffset, refcountClusters * cluster}} {
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

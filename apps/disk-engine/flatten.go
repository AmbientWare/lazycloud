package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
)

// mappedExtent is one range of `qemu-img map --output=json`.
type mappedExtent struct {
	Start  int64  `json:"start"`
	Length int64  `json:"length"`
	Depth  int    `json:"depth"`
	Zero   bool   `json:"zero"`
	Data   bool   `json:"data"`
	Offset *int64 `json:"offset"`
}

// uploadFlattened publishes layers[0..top] as one parentless raw layer
// holding the disk's contents without its zero ranges. qemu-img map names the
// layer file and offset holding each range, and uploadFlattened reads the
// range from there. It never writes a flattened copy, so publishing needs no
// space beyond the chain itself.
func uploadFlattened(ctx context.Context, store *objectStore, p diskPaths, layers []layer, generation int64) (publishResult, error) {
	top := len(layers) - 1
	path := p.layerPath(layers[top])
	virtualSize, err := layerVirtualSize(path, layers[top])
	if err != nil {
		return publishResult{}, err
	}
	// -U because the running daemon holds the chain open.
	out, err := runTool(ctx, toolImage, "map", "-U", "--output=json", "-f", layers[top].format(), path)
	if err != nil {
		return publishResult{}, err
	}
	var mapped []mappedExtent
	if err := json.Unmarshal([]byte(out), &mapped); err != nil {
		return publishResult{}, fmt.Errorf("parse qemu-img map of %s: %w", path, err)
	}
	files := map[int]*os.File{}
	defer func() {
		for _, file := range files {
			file.Close()
		}
	}()
	var runs []*layerRun
	for _, extent := range mapped {
		if !extent.Data || extent.Zero {
			continue
		}
		if extent.Depth < 0 || extent.Depth > top {
			return publishResult{}, fmt.Errorf("qemu-img map of %s names depth %d in a chain of %d layers", path, extent.Depth, top+1)
		}
		if extent.Offset == nil {
			return publishResult{}, fmt.Errorf("layer %s stores the range at %d compressed or encrypted", layers[top-extent.Depth].file(), extent.Start)
		}
		file, open := files[extent.Depth]
		if !open {
			if file, err = os.Open(p.layerPath(layers[top-extent.Depth])); err != nil {
				return publishResult{}, err
			}
			files[extent.Depth] = file
		}
		runs = appendPiece(runs, layerPiece{start: extent.Start, length: extent.Length, file: file, fileOffset: *extent.Offset})
	}
	return uploadLayer(ctx, store, layerManifest{
		DiskID: p.id, Generation: generation, ParentGeneration: 0,
		VirtualSizeBytes: virtualSize, LayerSizeBytes: virtualSize, Format: formatRaw,
	}, runs)
}

// layerVirtualSize is the size of the disk a layer presents.
func layerVirtualSize(path string, l layer) (int64, error) {
	if l.Raw {
		info, err := os.Stat(path)
		if err != nil {
			return 0, err
		}
		return info.Size(), nil
	}
	return qcow2VirtualSize(path)
}

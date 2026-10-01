package diskengine

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"sync/atomic"

	"golang.org/x/sync/errgroup"
	"golang.org/x/sys/unix"
)

// layerDownloadConcurrency bounds how many layers of a chain restore at once,
// each fetching up to transferConcurrency chunks.
const layerDownloadConcurrency = 4

const maxManifestBytes = 64 << 20

// layerRun is a contiguous range of a layer's contents that holds data. Its
// pieces say where each part of it is stored, which for a flattened chain is
// spread across several layer files.
type layerRun struct {
	start, length int64
	pieces        []layerPiece
}

type layerPiece struct {
	start, length int64
	file          *os.File
	fileOffset    int64
}

// ReadAt reads the run's contents at off, an offset in the layer.
func (r *layerRun) ReadAt(p []byte, off int64) (int, error) {
	n := 0
	for n < len(p) {
		at := off + int64(n)
		i := sort.Search(len(r.pieces), func(i int) bool {
			return r.pieces[i].start+r.pieces[i].length > at
		})
		if i == len(r.pieces) || r.pieces[i].start > at {
			return n, io.EOF
		}
		piece := r.pieces[i]
		want := min(int64(len(p)-n), piece.start+piece.length-at)
		read, err := piece.file.ReadAt(p[n:n+int(want)], piece.fileOffset+at-piece.start)
		n += read
		if err != nil {
			return n, fmt.Errorf("read %s: %w", piece.file.Name(), err)
		}
	}
	return n, nil
}

// appendPiece extends the last run when piece continues it, so data that is
// contiguous in the layer is chunked as one stream wherever it is stored.
func appendPiece(runs []*layerRun, piece layerPiece) []*layerRun {
	if len(runs) > 0 {
		last := runs[len(runs)-1]
		if last.start+last.length == piece.start {
			last.pieces = append(last.pieces, piece)
			last.length += piece.length
			return runs
		}
	}
	return append(runs, &layerRun{start: piece.start, length: piece.length, pieces: []layerPiece{piece}})
}

// fileRuns lists each allocated extent of file, so the holes a restored
// layer is full of are never read.
func fileRuns(file *os.File, size int64) ([]*layerRun, error) {
	fd := int(file.Fd()) //nolint:gosec // Descriptors fit an int.
	var runs []*layerRun
	offset := int64(0)
	for offset < size {
		start, err := unix.Seek(fd, offset, unix.SEEK_DATA)
		if errors.Is(err, unix.ENXIO) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("seek data in %s: %w", file.Name(), err)
		}
		end, err := unix.Seek(fd, start, unix.SEEK_HOLE)
		if err != nil {
			return nil, fmt.Errorf("seek hole in %s: %w", file.Name(), err)
		}
		end = min(end, size)
		runs = append(runs, &layerRun{start: start, length: end - start, pieces: []layerPiece{
			{start: start, length: end - start, file: file, fileOffset: start},
		}})
		offset = end
	}
	return runs, nil
}

// uploadFile stores a qcow2 layer file as generation's layer.
func uploadFile(ctx context.Context, store *objectStore, diskID, path string, generation, parent int64) (publishResult, error) {
	file, err := os.Open(path) //nolint:gosec // A layer path under the root.
	if err != nil {
		return publishResult{}, fmt.Errorf("open layer: %w", err)
	}
	defer func() { _ = file.Close() }() // Read only.
	info, err := file.Stat()
	if err != nil {
		return publishResult{}, fmt.Errorf("stat layer: %w", err)
	}
	// A layer sealed before the disk grew keeps its smaller size.
	virtualSize, err := qcow2VirtualSize(path)
	if err != nil {
		return publishResult{}, err
	}
	runs, err := fileRuns(file, info.Size())
	if err != nil {
		return publishResult{}, err
	}
	return uploadLayer(ctx, store, layerManifest{
		DiskID: diskID, Generation: generation, ParentGeneration: parent,
		VirtualSizeBytes: virtualSize, LayerSizeBytes: info.Size(), Format: formatQcow2,
	}, runs)
}

// uploadLayer stores every non-zero chunk of runs the disk does not already
// hold, then the manifest naming them. Each run starts a new chunk.
func uploadLayer(ctx context.Context, store *objectStore, manifest layerManifest, runs []*layerRun) (publishResult, error) {
	type located struct {
		chunk manifestChunk
		run   *layerRun
	}
	var chunks []manifestChunk
	unique := map[string]located{}
	cuts := newChunker()
	for _, run := range runs {
		err := cuts.split(io.NewSectionReader(run, run.start, run.length), run.start, func(span chunkSpan) error {
			if span.Zero {
				return nil
			}
			chunk := manifestChunk{Offset: span.Offset, Length: span.Length, SHA256: hex.EncodeToString(span.Sum[:])}
			chunks = append(chunks, chunk)
			if _, seen := unique[chunk.SHA256]; !seen {
				unique[chunk.SHA256] = located{chunk: chunk, run: run}
			}
			return nil
		})
		if err != nil {
			return publishResult{}, fmt.Errorf("chunk generation %d at %d: %w", manifest.Generation, run.start, err)
		}
	}

	var added atomic.Int64
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(transferConcurrency)
	for _, item := range unique {
		group.Go(func() error {
			chunk := item.chunk
			key := store.chunkKey(manifest.DiskID, chunk.SHA256)
			stored, err := store.exists(groupCtx, key)
			if err != nil || stored {
				return err
			}
			data := make([]byte, chunk.Length)
			if _, err := item.run.ReadAt(data, chunk.Offset); err != nil {
				return fmt.Errorf("read generation %d at %d: %w", manifest.Generation, chunk.Offset, err)
			}
			if sum := sha256.Sum256(data); hex.EncodeToString(sum[:]) != chunk.SHA256 {
				return fmt.Errorf("generation %d changed at %d while it was being published", manifest.Generation, chunk.Offset)
			}
			if err := store.put(groupCtx, key, data, "application/octet-stream"); err != nil {
				return err
			}
			added.Add(chunk.Length)
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return publishResult{}, fmt.Errorf("upload generation %d: %w", manifest.Generation, err)
	}

	manifest.Filesystem = diskFilesystem
	manifest.Chunks = chunks
	data, digest, err := encodeManifest(manifest)
	if err != nil {
		return publishResult{}, err
	}
	key := store.manifestKey(manifest.DiskID, manifest.Generation, digest)
	if err := store.put(ctx, key, data, "application/json"); err != nil {
		return publishResult{}, err
	}
	return publishResult{
		ManifestKey:      key,
		ManifestSHA256:   digest,
		StoredBytesAdded: added.Load(),
		Generation:       manifest.Generation,
		ParentGeneration: manifest.ParentGeneration,
	}, nil
}

// mappedExtent is one range of `qemu-img map --output=json`.
type mappedExtent struct {
	Start   int64  `json:"start"`
	Length  int64  `json:"length"`
	Depth   int    `json:"depth"`
	Zero    bool   `json:"zero"`
	Data    bool   `json:"data"`
	Present bool   `json:"present"`
	Offset  *int64 `json:"offset"`
}

func mapImage(ctx context.Context, path string, format layerFormat, shared bool) ([]mappedExtent, error) {
	args := []string{"map"}
	if shared {
		// The running daemon holds the chain open.
		args = append(args, "-U")
	}
	out, err := runTool(ctx, toolImage, append(args, "--output=json", "-f", string(format), path)...)
	if err != nil {
		return nil, err
	}
	var extents []mappedExtent
	if err := json.Unmarshal([]byte(out), &extents); err != nil {
		return nil, fmt.Errorf("parse qemu-img map of %s: %w", path, err)
	}
	return extents, nil
}

// uploadFlattened publishes layers as one parentless raw layer holding the
// disk's contents without its zero ranges. qemu-img map names the layer file
// and offset holding each range, and uploadFlattened reads the range from
// there. It never writes a flattened copy, so publishing needs no space
// beyond the chain itself.
func uploadFlattened(ctx context.Context, store *objectStore, p diskPaths, layers []layer, generation int64) (publishResult, error) {
	top := len(layers) - 1
	path := p.layerPath(layers[top])
	virtualSize, err := layerVirtualSize(path, layers[top])
	if err != nil {
		return publishResult{}, err
	}
	mapped, err := mapImage(ctx, path, layers[top].format(), true)
	if err != nil {
		return publishResult{}, err
	}
	files := map[int]*os.File{}
	defer func() {
		for _, file := range files {
			_ = file.Close() // Read only.
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
				return publishResult{}, fmt.Errorf("open layer: %w", err)
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

func fetchManifest(ctx context.Context, store *objectStore, entry Generation) (layerManifest, error) {
	data, err := store.get(ctx, entry.ManifestKey, maxManifestBytes)
	if err != nil {
		return layerManifest{}, err
	}
	manifest, err := decodeManifest(data, entry.ManifestSHA256)
	if err != nil {
		return manifest, fmt.Errorf("%s: %w", entry.ManifestKey, err)
	}
	return manifest, nil
}

// downloadLayer rebuilds a layer file byte for byte as a sparse file of the
// layer's size, with each stored chunk written at its offset. Zero regions
// were never stored and stay holes.
func downloadLayer(ctx context.Context, store *objectStore, manifest layerManifest, path string) (int64, error) {
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_TRUNC, 0o600) //nolint:gosec // A layer path under the root.
	if err != nil {
		return 0, fmt.Errorf("create layer: %w", err)
	}
	restored, err := func() (int64, error) {
		if err := file.Truncate(manifest.LayerSizeBytes); err != nil {
			return 0, fmt.Errorf("size layer: %w", err)
		}
		var restored atomic.Int64
		group, groupCtx := errgroup.WithContext(ctx)
		group.SetLimit(transferConcurrency)
		for _, chunk := range manifest.Chunks {
			group.Go(func() error {
				key := store.chunkKey(manifest.DiskID, chunk.SHA256)
				data, err := store.get(groupCtx, key, chunk.Length)
				if err != nil {
					return err
				}
				sum := sha256.Sum256(data)
				if int64(len(data)) != chunk.Length || hex.EncodeToString(sum[:]) != chunk.SHA256 {
					return fmt.Errorf("chunk %s does not match its digest or length", key)
				}
				if _, err := file.WriteAt(data, chunk.Offset); err != nil {
					return fmt.Errorf("write layer: %w", err)
				}
				restored.Add(chunk.Length)
				return nil
			})
		}
		if err := group.Wait(); err != nil {
			return 0, fmt.Errorf("download generation %d: %w", manifest.Generation, err)
		}
		if err := file.Sync(); err != nil {
			return 0, fmt.Errorf("sync layer: %w", err)
		}
		return restored.Load(), nil
	}()
	if closeErr := file.Close(); closeErr != nil && err == nil {
		return 0, fmt.Errorf("close layer: %w", closeErr)
	}
	return restored, err
}

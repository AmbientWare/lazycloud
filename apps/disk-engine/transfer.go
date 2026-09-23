package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"sync/atomic"

	"golang.org/x/sync/errgroup"
	"golang.org/x/sys/unix"
)

const transferConcurrency = 8

type publishResult struct {
	ManifestKey      string `json:"manifest_key"`
	ManifestSHA256   string `json:"manifest_sha256"`
	StoredBytesAdded int64  `json:"stored_bytes_added"`
	Generation       int64  `json:"generation"`
	ParentGeneration int64  `json:"parent_generation"`
}

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
			return n, err
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
	fd := int(file.Fd())
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
	file, err := os.Open(path)
	if err != nil {
		return publishResult{}, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return publishResult{}, err
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
	for _, run := range runs {
		err := chunkStream(io.NewSectionReader(run, run.start, run.length), run.start, func(span chunkSpan) error {
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
			key := diskChunkKey(manifest.DiskID, chunk.SHA256)
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
		return publishResult{}, err
	}

	manifest.Filesystem = diskFilesystem
	manifest.Chunks = chunks
	data, digest, err := encodeManifest(manifest)
	if err != nil {
		return publishResult{}, err
	}
	key := diskManifestKey(manifest.DiskID, manifest.Generation)
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

const maxManifestBytes = 64 << 20

func fetchManifest(ctx context.Context, store *objectStore, entry chainEntry) (layerManifest, error) {
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
	file, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return 0, err
	}
	defer file.Close()
	if err := file.Truncate(manifest.LayerSizeBytes); err != nil {
		return 0, err
	}
	var restored atomic.Int64
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(transferConcurrency)
	for _, chunk := range manifest.Chunks {
		group.Go(func() error {
			key := diskChunkKey(manifest.DiskID, chunk.SHA256)
			data, err := store.get(groupCtx, key, chunk.Length)
			if err != nil {
				return err
			}
			sum := sha256.Sum256(data)
			if int64(len(data)) != chunk.Length || hex.EncodeToString(sum[:]) != chunk.SHA256 {
				return fmt.Errorf("chunk %s does not match its digest or length", key)
			}
			if _, err := file.WriteAt(data, chunk.Offset); err != nil {
				return err
			}
			restored.Add(chunk.Length)
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return 0, err
	}
	if err := file.Sync(); err != nil {
		return 0, err
	}
	return restored.Load(), nil
}

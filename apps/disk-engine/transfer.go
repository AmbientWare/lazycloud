package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"sync/atomic"

	"golang.org/x/sync/errgroup"
)

const transferConcurrency = 8

type publishResult struct {
	ManifestKey      string `json:"manifest_key"`
	ManifestSHA256   string `json:"manifest_sha256"`
	StoredBytesAdded int64  `json:"stored_bytes_added"`
	Generation       int64  `json:"generation"`
	ParentGeneration int64  `json:"parent_generation"`
}

// uploadLayer stores path as generation's layer: every non-zero chunk the
// disk does not already hold, then the manifest naming them.
func uploadLayer(ctx context.Context, store *objectStore, diskID, path string, virtualSize, generation, parent int64) (publishResult, error) {
	file, err := os.Open(path)
	if err != nil {
		return publishResult{}, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return publishResult{}, err
	}

	var chunks []manifestChunk
	unique := map[string]manifestChunk{}
	err = chunkFile(file, info.Size(), func(span chunkSpan) error {
		if span.Zero {
			return nil
		}
		chunk := manifestChunk{Offset: span.Offset, Length: span.Length, SHA256: hex.EncodeToString(span.Sum[:])}
		chunks = append(chunks, chunk)
		if _, seen := unique[chunk.SHA256]; !seen {
			unique[chunk.SHA256] = chunk
		}
		return nil
	})
	if err != nil {
		return publishResult{}, fmt.Errorf("chunk %s: %w", path, err)
	}

	var added atomic.Int64
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(transferConcurrency)
	for _, chunk := range unique {
		group.Go(func() error {
			key := diskChunkKey(diskID, chunk.SHA256)
			stored, err := store.exists(groupCtx, key)
			if err != nil || stored {
				return err
			}
			data := make([]byte, chunk.Length)
			if _, err := file.ReadAt(data, chunk.Offset); err != nil {
				return fmt.Errorf("read %s at %d: %w", path, chunk.Offset, err)
			}
			if sum := sha256.Sum256(data); hex.EncodeToString(sum[:]) != chunk.SHA256 {
				return fmt.Errorf("%s changed while it was being published", path)
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

	manifest := layerManifest{
		DiskID:           diskID,
		Generation:       generation,
		ParentGeneration: parent,
		VirtualSizeBytes: virtualSize,
		LayerSizeBytes:   info.Size(),
		Filesystem:       diskFilesystem,
		Chunks:           chunks,
	}
	data, digest, err := encodeManifest(manifest)
	if err != nil {
		return publishResult{}, err
	}
	key := diskManifestKey(diskID, generation)
	if err := store.put(ctx, key, data, "application/json"); err != nil {
		return publishResult{}, err
	}
	return publishResult{
		ManifestKey:      key,
		ManifestSHA256:   digest,
		StoredBytesAdded: added.Load(),
		Generation:       generation,
		ParentGeneration: parent,
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

// downloadLayer rebuilds a layer file byte for byte: a sparse file of the
// layer's size with each stored chunk written at its offset. Zero regions were
// never stored and stay holes.
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

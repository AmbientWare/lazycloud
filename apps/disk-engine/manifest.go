package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
)

// Object keys and the manifest mirror shared.disks field for field; the
// control plane and this engine read each other's objects.
const diskObjectPrefix = "disks"

func diskChunkKey(diskID, sum string) string {
	return fmt.Sprintf("%s/%s/chunks/%s/%s", diskObjectPrefix, diskID, sum[:2], sum)
}

func diskManifestKey(diskID string, generation int64) string {
	return fmt.Sprintf("%s/%s/manifests/%012d.json", diskObjectPrefix, diskID, generation)
}

const diskFilesystem = "ext4"

type manifestChunk struct {
	Offset int64  `json:"offset"`
	Length int64  `json:"length"`
	SHA256 string `json:"sha256"`
}

type layerManifest struct {
	DiskID           string `json:"disk_id"`
	Generation       int64  `json:"generation"`
	ParentGeneration int64  `json:"parent_generation"`
	VirtualSizeBytes int64  `json:"virtual_size_bytes"`
	LayerSizeBytes   int64  `json:"layer_size_bytes"`
	// Format is qcow2 for a layer over its parent, or raw for a flattened
	// generation's contents. Absent means qcow2.
	Format     string          `json:"format,omitempty"`
	Filesystem string          `json:"filesystem"`
	Chunks     []manifestChunk `json:"chunks"`
}

var sha256Pattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

func encodeManifest(manifest layerManifest) ([]byte, string, error) {
	if manifest.Chunks == nil {
		manifest.Chunks = []manifestChunk{}
	}
	data, err := json.Marshal(manifest)
	if err != nil {
		return nil, "", err
	}
	sum := sha256.Sum256(data)
	return data, hex.EncodeToString(sum[:]), nil
}

// decodeManifest checks the bytes against the digest the control plane
// recorded before trusting anything in them.
func decodeManifest(data []byte, wantSHA256 string) (layerManifest, error) {
	var manifest layerManifest
	sum := sha256.Sum256(data)
	if got := hex.EncodeToString(sum[:]); got != wantSHA256 {
		return manifest, fmt.Errorf("manifest sha256 is %s, recorded %s", got, wantSHA256)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return manifest, fmt.Errorf("parse manifest: %w", err)
	}
	if manifest.Generation <= 0 || manifest.ParentGeneration < 0 ||
		manifest.VirtualSizeBytes <= 0 || manifest.LayerSizeBytes < 0 {
		return manifest, fmt.Errorf("manifest for generation %d has invalid sizes or generations", manifest.Generation)
	}
	switch manifest.Format {
	case "", formatQcow2:
		manifest.Format = formatQcow2
	case formatRaw:
		if manifest.ParentGeneration != 0 || manifest.LayerSizeBytes != manifest.VirtualSizeBytes {
			return manifest, fmt.Errorf("raw generation %d must have no parent and span the disk", manifest.Generation)
		}
	default:
		return manifest, fmt.Errorf("manifest for generation %d has unknown format %q", manifest.Generation, manifest.Format)
	}
	for _, chunk := range manifest.Chunks {
		if chunk.Offset < 0 || chunk.Length <= 0 || chunk.Length > maxChunkBytes ||
			chunk.Offset+chunk.Length > manifest.LayerSizeBytes || !sha256Pattern.MatchString(chunk.SHA256) {
			return manifest, fmt.Errorf("manifest for generation %d has an invalid chunk at offset %d", manifest.Generation, chunk.Offset)
		}
	}
	return manifest, nil
}

// chainEntry is one CHAIN.json element: a published generation to restore,
// listed base first.
type chainEntry struct {
	Generation     int64  `json:"generation"`
	ManifestKey    string `json:"manifest_key"`
	ManifestSHA256 string `json:"manifest_sha256"`
}

func readChain(path string) ([]chainEntry, error) {
	var chain []chainEntry
	if err := readJSONFile(path, &chain); err != nil {
		return nil, err
	}
	previous := int64(0)
	for _, entry := range chain {
		if entry.Generation <= previous {
			return nil, fmt.Errorf("%s: generations must increase from the base, got %d after %d", path, entry.Generation, previous)
		}
		if entry.ManifestKey == "" || !sha256Pattern.MatchString(entry.ManifestSHA256) {
			return nil, fmt.Errorf("%s: generation %d needs a manifest_key and a sha256 manifest_sha256", path, entry.Generation)
		}
		previous = entry.Generation
	}
	return chain, nil
}

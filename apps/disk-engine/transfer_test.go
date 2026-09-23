package main

import (
	"bytes"
	"context"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func TestManifestRoundTrip(t *testing.T) {
	manifest := layerManifest{
		DiskID: "d1", Generation: 3, ParentGeneration: 2,
		VirtualSizeBytes: 1 << 30, LayerSizeBytes: 5 << 20, Filesystem: diskFilesystem,
		Chunks: []manifestChunk{{Offset: 0, Length: 2 << 20, SHA256: fmt.Sprintf("%064x", 1)}},
	}
	data, digest, err := encodeManifest(manifest)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := decodeManifest(data, digest)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(decoded, manifest) {
		t.Fatalf("decoded %+v, want %+v", decoded, manifest)
	}
	tampered := bytes.Replace(data, []byte(`"generation":3`), []byte(`"generation":4`), 1)
	if _, err := decodeManifest(tampered, digest); err == nil {
		t.Fatal("a manifest that does not match its recorded digest was accepted")
	}
}

// TestRestoreReproducesLayer needs an S3-compatible bucket, named by a
// STORE.json path in LAZYCLOUD_DISK_TEST_STORE.
func TestRestoreReproducesLayer(t *testing.T) {
	storePath := os.Getenv("LAZYCLOUD_DISK_TEST_STORE")
	if storePath == "" {
		t.Skip("LAZYCLOUD_DISK_TEST_STORE names no STORE.json")
	}
	store, err := openStore(storePath)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	dir := t.TempDir()
	diskID := fmt.Sprintf("test-%d", time.Now().UnixNano())

	// Random data around a zero run and a hole, the shapes a qcow2 layer has.
	source := filepath.Join(dir, "source.qcow2")
	file, err := os.Create(source)
	if err != nil {
		t.Fatal(err)
	}
	random := make([]byte, 20<<20)
	rand.NewChaCha8([32]byte{9}).Read(random)
	file.Write(random[:9<<20])
	file.Write(make([]byte, 10<<20))
	file.WriteAt(random[9<<20:], 40<<20)
	file.Close()

	published, err := uploadLayer(ctx, store, diskID, source, 1<<30, 1, 0)
	if err != nil {
		t.Fatal(err)
	}
	// Chunks straddling the zero run carry some zeros; whole zero chunks and
	// the hole are never stored.
	if published.StoredBytesAdded < 20<<20 || published.StoredBytesAdded >= 30<<20 {
		t.Fatalf("stored %d new bytes for 20MiB of data beside 10MiB of zeros", published.StoredBytesAdded)
	}
	again, err := uploadLayer(ctx, store, diskID, source, 1<<30, 2, 1)
	if err != nil {
		t.Fatal(err)
	}
	if again.StoredBytesAdded != 0 {
		t.Fatalf("republishing identical content stored %d bytes", again.StoredBytesAdded)
	}

	manifest, err := fetchManifest(ctx, store, chainEntry{
		Generation: 1, ManifestKey: published.ManifestKey, ManifestSHA256: published.ManifestSHA256,
	})
	if err != nil {
		t.Fatal(err)
	}
	restoredPath := filepath.Join(dir, "restored.qcow2")
	if _, err := downloadLayer(ctx, store, manifest, restoredPath); err != nil {
		t.Fatal(err)
	}
	want, _ := os.ReadFile(source)
	got, _ := os.ReadFile(restoredPath)
	if !bytes.Equal(want, got) {
		t.Fatalf("restored layer (%d bytes) differs from the published one (%d bytes)", len(got), len(want))
	}
}

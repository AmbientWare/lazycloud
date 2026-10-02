package diskengine

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/google/uuid"
)

// A real qcow2 layer published to the bucket restores byte for byte, stores
// no zero chunks, and a second publish of the same content stores nothing.
func TestRestoreReproducesLayer(t *testing.T) {
	requireTools(t, toolImage, "qemu-io")
	store := testStore(t)
	objects := mustOpenStore(t, store)
	ctx := t.Context()
	diskID := uuid.NewString()

	source := filepath.Join(t.TempDir(), "source.qcow2")
	run(t, toolImage, "create", "-q", "-f", "qcow2", source, "256M")
	run(t, "qemu-io", "-f", "qcow2",
		"-c", "write -P 0xab 1M 9M",
		"-c", "write -z 10M 10M",
		"-c", "write -P 0xcd 40M 11M", source)

	published, err := uploadFile(ctx, objects, diskID, source, 1, 0)
	if err != nil {
		t.Fatal(err)
	}
	// Chunks straddling qcow2 metadata carry a little of it; the zeroed range
	// is a metadata flag, never stored data.
	if published.StoredBytesAdded < 20<<20 || published.StoredBytesAdded >= 24<<20 {
		t.Fatalf("stored %d new bytes for 20MiB of data beside 10MiB of zeros", published.StoredBytesAdded)
	}
	// The key carries the digest, so another manifest of generation 1 could
	// never replace this one.
	if published.ManifestKey != store.Prefix+"disks/"+diskID+"/manifests/000000000001-"+published.ManifestSHA256+".json" {
		t.Fatalf("manifest stored at %s", published.ManifestKey)
	}
	again, err := uploadFile(ctx, objects, diskID, source, 2, 1)
	if err != nil {
		t.Fatal(err)
	}
	if again.StoredBytesAdded != 0 {
		t.Fatalf("republishing identical content stored %d bytes", again.StoredBytesAdded)
	}

	manifest, err := fetchManifest(ctx, objects, Generation{
		Generation: 1, ManifestKey: published.ManifestKey, ManifestSHA256: published.ManifestSHA256,
	})
	if err != nil {
		t.Fatal(err)
	}
	if manifest.VirtualSizeBytes != 256<<20 || manifest.Format != formatQcow2 {
		t.Fatalf("manifest describes a %d-byte %s layer", manifest.VirtualSizeBytes, manifest.Format)
	}
	restored := filepath.Join(t.TempDir(), "restored.qcow2")
	if _, err := downloadLayer(ctx, objects, manifest, restored); err != nil {
		t.Fatal(err)
	}
	want, err := os.ReadFile(source)
	if err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(restored)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(want, got) {
		t.Fatalf("restored layer (%d bytes) differs from the published one (%d bytes)", len(got), len(want))
	}
	run(t, toolImage, "check", "-q", restored)
}

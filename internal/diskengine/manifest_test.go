package diskengine

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"testing"
)

func TestManifestRoundTrip(t *testing.T) {
	manifest := layerManifest{
		DiskID: "d1", Generation: 3, ParentGeneration: 2,
		VirtualSizeBytes: 1 << 30, LayerSizeBytes: 5 << 20, Format: formatQcow2, Filesystem: diskFilesystem,
		Chunks: []manifestChunk{{Offset: 0, Length: 2 << 20, SHA256: fmt.Sprintf("%064x", 1)}},
	}
	data, digest, err := encodeManifest(manifest)
	if err != nil {
		t.Fatal(err)
	}
	// The stored format other engines and the control plane read.
	want := `{"disk_id":"d1","generation":3,"parent_generation":2,"virtual_size_bytes":1073741824,` +
		`"layer_size_bytes":5242880,"format":"qcow2","filesystem":"ext4","chunks":[{"offset":0,"length":2097152,` +
		`"sha256":"0000000000000000000000000000000000000000000000000000000000000001"}]}`
	if string(data) != want {
		t.Fatalf("encoded\n%s\nwant\n%s", data, want)
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

func TestChainMustIncreaseAndNameDigests(t *testing.T) {
	digest := fmt.Sprintf("%064x", 7)
	for name, chain := range map[string][]Generation{
		"repeated generation": {{1, "k1", digest}, {1, "k2", digest}},
		"missing key":         {{1, "", digest}},
		"bad digest":          {{1, "k1", "abc"}},
	} {
		if err := validateChain(chain); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: got %v, want ErrInvalid", name, err)
		}
	}
	if err := validateChain([]Generation{{1, "k1", digest}, {4, "k4", digest}}); err != nil {
		t.Fatal(err)
	}
}

// A layer whose manifest no restore would read is refused at publish, so no
// generation is recorded that cannot be attached.
func TestManifestRestoresCannotReadIsRefused(t *testing.T) {
	chunk := manifestChunk{Offset: 1 << 40, Length: minChunkBytes, SHA256: fmt.Sprintf("%064x", 1)}
	entry, err := json.Marshal(chunk)
	if err != nil {
		t.Fatal(err)
	}
	chunks := make([]manifestChunk, maxManifestBytes/len(entry)+1)
	for i := range chunks {
		chunks[i] = chunk
	}
	manifest := layerManifest{DiskID: "d1", Generation: 1, VirtualSizeBytes: 1 << 40, LayerSizeBytes: 1 << 40, Filesystem: diskFilesystem, Chunks: chunks}
	if _, _, err := encodeManifest(manifest); !errors.Is(err, ErrManifestTooLarge) {
		t.Fatalf("encoding a %d-chunk manifest returned %v, want ErrManifestTooLarge", len(chunks), err)
	}
	manifest.Chunks = chunks[:len(chunks)/2]
	if _, _, err := encodeManifest(manifest); err != nil {
		t.Fatal(err)
	}
}

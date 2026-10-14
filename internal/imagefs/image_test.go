//go:build hostruntime

package imagefs

import (
	"io"
	"runtime"
	"testing"

	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/remote"
)

// Every layer of python:3.12-slim reads back as the tar holds it, and its
// index names the layer by the image's diff_id.
func TestPythonLayersRoundTrip(t *testing.T) {
	t.Parallel()
	ref, err := name.ParseReference("python:3.12-slim")
	if err != nil {
		t.Fatal(err)
	}
	img, err := remote.Image(ref, remote.WithContext(t.Context()), remote.WithPlatform(v1.Platform{OS: "linux", Architecture: runtime.GOARCH}))
	if err != nil {
		t.Fatal(err)
	}
	layers, err := img.Layers()
	if err != nil || len(layers) < 2 {
		t.Fatalf("%d layers: %v", len(layers), err)
	}
	for i, l := range layers {
		diffID, err := l.DiffID()
		if err != nil {
			t.Fatal(err)
		}
		r, err := l.Uncompressed()
		if err != nil {
			t.Fatal(err)
		}
		layer, err := io.ReadAll(r)
		_ = r.Close()
		if err != nil {
			t.Fatal(err)
		}
		ix, data := roundTrip(t, layer)
		if string(ix.Layer) != diffID.String() {
			t.Fatalf("layer %d digest %s, image diff_id %s", i, ix.Layer, diffID)
		}
		checkAgainstTar(t, layer, ix, serve(t, data))
		t.Logf("layer %d: %d bytes tar, %d entries, %d frames, %d data bytes", i, len(layer), len(ix.Entries), len(ix.Frames), ix.DataSize)
	}
}

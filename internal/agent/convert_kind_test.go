package agent

import (
	"errors"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/registry"
	"github.com/google/go-containerregistry/pkg/v1/random"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/types"
)

// A host that cannot write its data file fails the build for itself; only
// a layer's own content fails the image.
func TestHostErrorsAreNotLayerContent(t *testing.T) {
	server := httptest.NewServer(registry.New())
	t.Cleanup(server.Close)
	repository := strings.TrimPrefix(server.URL, "http://") + "/lazycloud/images"
	layer, err := random.Layer(1024, types.DockerLayer)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := layer.Digest()
	if err != nil {
		t.Fatal(err)
	}
	ref, err := name.NewDigest(repository+"@"+digest.String(), name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	if err := remote.WriteLayer(ref.Context(), layer, remote.WithContext(t.Context())); err != nil {
		t.Fatal(err)
	}
	diffID, err := layer.DiffID()
	if err != nil {
		t.Fatal(err)
	}

	missing := &layerPublish{dir: filepath.Join(t.TempDir(), "gone")}
	if _, _, err := missing.convertLayer(t.Context(), ref, authn.Anonymous, diffID.String()); err == nil || errors.Is(err, errLayerContent) {
		t.Fatalf("an unwritable data directory gave %v, want a host error", err)
	}
	mismatched := &layerPublish{dir: t.TempDir()}
	if _, _, err := mismatched.convertLayer(t.Context(), ref, authn.Anonymous, "sha256:"+strings.Repeat("0", 64)); !errors.Is(err, errLayerContent) {
		t.Fatalf("a layer that is not its config's diff_id gave %v, want its content refused", err)
	}
}

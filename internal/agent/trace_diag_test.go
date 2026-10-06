package agent

import (
	"testing"

	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/core/images"
	"github.com/containerd/platforms"
	"github.com/distribution/reference"
	digestid "github.com/opencontainers/image-spec/identity"

	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// Temporary: how the host came to hold the test image without lazy layers.
func TestTraceDiagnostic(t *testing.T) {
	ctx := t.Context()
	ctrd, err := containerd.New(containerdSocket, containerd.WithDefaultNamespace("moby"))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ctrd.Close() }()
	for _, image := range lazyImages {
		named, _ := reference.ParseDockerRef(image)
		img, err := ctrd.GetImage(ctx, named.String())
		if err != nil {
			t.Logf("%s: %v", image, err)
			continue
		}
		t.Logf("%s: created %s updated %s labels %v", image, img.Metadata().CreatedAt, img.Metadata().UpdatedAt, img.Metadata().Labels)
		manifest, err := images.Manifest(ctx, ctrd.ContentStore(), img.Target(), platforms.Default())
		if err != nil {
			t.Logf("%s: manifest %v", image, err)
			continue
		}
		for _, layer := range manifest.Layers {
			info, err := ctrd.ContentStore().Info(ctx, layer.Digest)
			t.Logf("  blob %s: created %s err %v", layer.Digest, info.CreatedAt, err)
		}
		diffs, err := img.RootFS(ctx)
		if err != nil {
			t.Logf("%s: rootfs %v", image, err)
			continue
		}
		snapshots := ctrd.SnapshotService(layersource.Snapshotter)
		for n := range diffs {
			chain := digestid.ChainID(diffs[:n+1]).String()
			info, err := snapshots.Stat(ctx, chain)
			t.Logf("  snapshot %s: kind %v created %s labels %v err %v", chain, info.Kind, info.Created, info.Labels, err)
		}
	}
	t.Error("diagnostic")
}

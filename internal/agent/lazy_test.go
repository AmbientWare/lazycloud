package agent

import (
	"encoding/json"
	"os"
	"testing"
	"time"

	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/core/images"
	cerrdefs "github.com/containerd/errdefs"
	"github.com/containerd/platforms"
	"github.com/distribution/reference"
	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// A converted image pulls through containerd with no layer downloaded and
// runs under Docker from its lazily read layers. It needs a host set up by
// install-snapshotter, as root: LAZYCLOUD_TEST_LAZY_IMAGE names the image
// in a registry, LAZYCLOUD_TEST_LAZY_GRANTS a JSON file of its layers'
// grants and LAZYCLOUD_TEST_LAZY_COMMAND the command that must succeed in
// it under LAZYCLOUD_TEST_RUNTIME (runsc by default).
func TestLazyPullDownloadsNoLayer(t *testing.T) {
	image, grantsFile := os.Getenv("LAZYCLOUD_TEST_LAZY_IMAGE"), os.Getenv("LAZYCLOUD_TEST_LAZY_GRANTS")
	if image == "" || grantsFile == "" {
		t.Skip("set LAZYCLOUD_TEST_LAZY_IMAGE and LAZYCLOUD_TEST_LAZY_GRANTS on a host with the snapshotter")
	}
	ctx := t.Context()
	raw, err := os.ReadFile(grantsFile)
	if err != nil {
		t.Fatal(err)
	}
	var stored []struct {
		DiffID    imagefs.Digest `json:"diff_id"`
		IndexURL  string         `json:"index_url"`
		DataURL   string         `json:"data_url"`
		ExpiresAt time.Time      `json:"expires_at"`
	}
	if err := json.Unmarshal(raw, &stored); err != nil {
		t.Fatal(err)
	}
	grants := make([]layersource.Grant, len(stored))
	for i, g := range stored {
		grants[i] = layersource.Grant{Layer: g.DiffID, IndexURL: g.IndexURL, DataURL: g.DataURL, ExpiresAt: g.ExpiresAt}
	}
	sources, err := layersource.Dial(layersource.Socket)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sources.Close() }()
	if err := sources.Grant(ctx, grants); err != nil {
		t.Fatal(err)
	}

	ctrd, err := containerd.New(containerdSocket, containerd.WithDefaultNamespace("moby"))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ctrd.Close() }()
	docker, err := client.New(client.FromEnv)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = docker.Close() }()
	cache := &imageCache{docker: docker, containerd: ctrd}
	named, err := reference.ParseDockerRef(image)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := docker.ImageRemove(ctx, image, client.ImageRemoveOptions{Force: true}); err != nil && !cerrdefs.IsNotFound(err) {
		t.Fatal(err)
	}

	began := time.Now()
	pulled, err := cache.ensureLazy(ctx, image, nil, "")
	if err != nil || !pulled {
		t.Fatalf("lazy pull: pulled %v, %v", pulled, err)
	}
	t.Logf("pull took %v", time.Since(began))
	img, err := ctrd.GetImage(ctx, named.String())
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := images.Manifest(ctx, ctrd.ContentStore(), img.Target(), platforms.Default())
	if err != nil {
		t.Fatal(err)
	}
	for _, layer := range manifest.Layers {
		if _, err := ctrd.ContentStore().Info(ctx, layer.Digest); !cerrdefs.IsNotFound(err) {
			t.Fatalf("layer blob %s is in the content store: %v", layer.Digest, err)
		}
	}
	if again, err := cache.ensureLazy(ctx, image, nil, ""); err != nil || again {
		t.Fatalf("a second pull: pulled %v, %v", again, err)
	}

	runtime := os.Getenv("LAZYCLOUD_TEST_RUNTIME")
	if runtime == "" {
		runtime = "runsc"
	}
	command := os.Getenv("LAZYCLOUD_TEST_LAZY_COMMAND")
	if command == "" {
		command = "true"
	}
	began = time.Now()
	created, err := docker.ContainerCreate(ctx, client.ContainerCreateOptions{
		Image:      image,
		Config:     &containertypes.Config{Image: image, Cmd: []string{"sh", "-c", command}},
		HostConfig: &containertypes.HostConfig{Runtime: runtime},
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		_, _ = docker.ContainerRemove(ctx, created.ID, client.ContainerRemoveOptions{Force: true})
	}()
	if _, err := docker.ContainerStart(ctx, created.ID, client.ContainerStartOptions{}); err != nil {
		t.Fatal(err)
	}
	wait := docker.ContainerWait(ctx, created.ID, client.ContainerWaitOptions{})
	select {
	case result := <-wait.Result:
		if result.StatusCode != 0 {
			t.Fatalf("%q exited %d under %s", command, result.StatusCode, runtime)
		}
	case err := <-wait.Error:
		t.Fatal(err)
	}
	t.Logf("run took %v", time.Since(began))
}

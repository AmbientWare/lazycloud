//go:build hostruntime

package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"testing"
	"time"

	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/core/images"
	cerrdefs "github.com/containerd/errdefs"
	"github.com/containerd/platforms"
	"github.com/distribution/reference"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
)

// lazyImages are the workload and platform images the tests start from a
// registry. Every host reads images through its snapshotter, so TestMain
// converts their layers into layerBucket, as the server does, and start
// commands and sessions carry the grants it would send.
var lazyImages = []string{testImage, alpineImage, testDockerImage, platformimages.Builder, platformimages.Mount}

// imageLayers holds the layer grants of each of lazyImages.
var imageLayers = map[string][]*imagefsproto.LayerGrant{}

// layerBucket is the test store's bucket the test pairs go to, made and
// removed by TestMain.
var layerBucket string //nolint:gochecknoglobals // Set once in TestMain.

// convertLazyImages fills imageLayers with grants for twelve hours. The
// conversion runs in imagefstest's command, outside this binary's race
// instrumentation, which makes it many times slower.
func convertLazyImages(ctx context.Context, endpoint, region, bucket, accessKeyID, secretAccessKey string) error {
	convert := exec.CommandContext(ctx, "go", "run", "../imagefs/imagefstest/convert",
		"-endpoint", endpoint, "-region", region, "-bucket", bucket, "-prefix", "agent-test/layers/", "-expires", "12h")
	convert.Args = append(convert.Args, lazyImages...)
	convert.Env = append(os.Environ(), "AWS_ACCESS_KEY_ID="+accessKeyID, "AWS_SECRET_ACCESS_KEY="+secretAccessKey)
	convert.Stderr = os.Stderr
	out, err := convert.Output()
	if err != nil {
		return fmt.Errorf("convert the test images: %w", err)
	}
	var grants map[string][]json.RawMessage
	if err := json.Unmarshal(out, &grants); err != nil {
		return fmt.Errorf("read the test images' grants: %w", err)
	}
	for _, ref := range lazyImages {
		for _, encoded := range grants[ref] {
			grant := &imagefsproto.LayerGrant{}
			if err := protojson.Unmarshal(encoded, grant); err != nil {
				return fmt.Errorf("read a grant of %s: %w", ref, err)
			}
			imageLayers[ref] = append(imageLayers[ref], grant)
		}
		if len(imageLayers[ref]) == 0 {
			return fmt.Errorf("%s converted to no layers", ref)
		}
	}
	return nil
}

// withImage points a start at image and carries its grants.
func withImage(start *hostproto.ServerMessage, image string) *hostproto.ServerMessage {
	start.GetStart().Image = image
	start.GetStart().Layers = imageLayers[image]
	return start
}

// noLayerSince fails if any layer blob of image reached containerd's
// content store since: a lazy pull downloads manifests and configs only.
// containerd shares content across namespaces, so a blob an earlier pull
// stored may be present.
func noLayerSince(t *testing.T, image string, since time.Time) {
	t.Helper()
	ctx := t.Context()
	ctrd, err := containerd.New(containerdSocket, containerd.WithDefaultNamespace("moby"))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ctrd.Close() }()
	named, err := reference.ParseDockerRef(image)
	if err != nil {
		t.Fatal(err)
	}
	img, err := ctrd.GetImage(ctx, named.String())
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := images.Manifest(ctx, ctrd.ContentStore(), img.Target(), platforms.Default())
	if err != nil {
		t.Fatal(err)
	}
	for _, layer := range manifest.Layers {
		info, err := ctrd.ContentStore().Info(ctx, layer.Digest)
		if err == nil && !info.CreatedAt.Before(since) {
			t.Fatalf("the pull of %s downloaded layer blob %s", image, layer.Digest)
		}
		if err != nil && !cerrdefs.IsNotFound(err) {
			t.Fatal(err)
		}
	}
}

// TestStartupTracesEndWithTheFirstTask: the host's snapshotter traces a
// start until its first task, request or command ends, the agent reports
// the frames read, and a later start prefetches them. No other package
// unpacks this image without the snapshotter, so its layers mount lazily
// and reads reach FUSE.
func TestStartupTracesEndWithTheFirstTask(t *testing.T) {
	t.Parallel()
	const image = alpineImage
	e := newEnv(t)
	e.startAgent()
	s := e.session()
	command := &hostproto.PodWorkload{Command: []string{"python3", "-c", "import email, http.client, json, sqlite3"}}

	start := withImage(e.podCommand(command), image)
	id := start.GetStart().GetContainerId()
	start.GetStart().RecordTrace = true
	s.send(t, start)
	reported := s.until(t, 120*time.Second, func(m *hostproto.HostMessage) bool { return m.GetStartupTrace() != nil }).GetStartupTrace()
	if reported.GetContainerId() != id || len(reported.GetTrace().GetReads()) == 0 {
		t.Fatalf("the host reported %v", reported)
	}
	s.phase(t, id, exited)

	next := withImage(e.podCommand(command), image)
	next.GetStart().Prefetch = reported.GetTrace()
	s.send(t, next)
	if exit := s.phase(t, next.GetStart().GetContainerId(), exited).GetExit(); exit.GetReason() != hostproto.ExitReason_EXIT_REASON_EXITED || exit.GetExitCode() != 0 {
		t.Fatalf("a start prefetching the trace exited %v", exit)
	}
}

//go:build hostruntime

package agent

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefstest"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/core/images"
	cerrdefs "github.com/containerd/errdefs"
	"github.com/distribution/reference"
	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/uuid"
	"github.com/moby/moby/client"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// removeImage deletes image from Docker's containerd and collects the
// snapshots no other image uses.
func removeImage(t *testing.T, ctrd *containerd.Client, image string) {
	t.Helper()
	named, err := reference.ParseDockerRef(image)
	if err != nil {
		t.Fatal(err)
	}
	err = ctrd.ImageService().Delete(context.WithoutCancel(t.Context()), named.String(), images.SynchronousDelete())
	if err != nil && !cerrdefs.IsNotFound(err) {
		t.Fatal(err)
	}
}

// TestPlatformAndTenantImagesReadThroughTheirOwnGrants: a tenant's image
// on the mount image's base is pulled first, so the snapshot of the shared
// base layer is the one its pull committed. Once the tenant's pairs are
// gone, a network holder, which runs the mount image, still starts: it
// reads the base through the grant the session gave for the platform
// image, which outlives the tenant's.
func TestPlatformAndTenantImagesReadThroughTheirOwnGrants(t *testing.T) {
	e := newEnv(t)
	ctx := t.Context()
	ctrd, err := containerd.New(containerdSocket, containerd.WithDefaultNamespace("moby"))
	if err != nil {
		t.Fatal(err)
	}
	// Registered first, so it runs after the cleanups that use the client.
	t.Cleanup(func() { _ = ctrd.Close() })

	base, err := imagefstest.Image(ctx, platformimages.Mount, v1.Platform{OS: "linux", Architecture: "amd64"})
	if err != nil {
		t.Fatal(err)
	}
	// The tenant adds a layer TestMain converted, so its pairs are copies.
	alpine, err := imagefstest.Image(ctx, alpineImage, v1.Platform{OS: "linux", Architecture: "amd64"})
	if err != nil {
		t.Fatal(err)
	}
	alpineLayers, err := alpine.Layers()
	if err != nil {
		t.Fatal(err)
	}
	tenant, err := mutate.AppendLayers(base, alpineLayers[0])
	if err != nil {
		t.Fatal(err)
	}
	registry := startTestRegistry(t)
	tenantRef := pushTestImage(t, registry+"/tenant/app", tenant)
	t.Cleanup(func() { removeImage(t, ctrd, tenantRef) })
	config, err := base.ConfigFile()
	if err != nil {
		t.Fatal(err)
	}
	if len(config.RootFS.DiffIDs) != 1 {
		t.Fatalf("the mount image has %d layers; the test shares its only one", len(config.RootFS.DiffIDs))
	}
	// A single layer's chain ID is its diff_id.
	shared := config.RootFS.DiffIDs[0].String()

	// No snapshot of the base may stay from an earlier test.
	removeImage(t, ctrd, platformimages.Mount)
	snapshots := ctrd.SnapshotService(layersource.Snapshotter)
	if _, err := snapshots.Stat(ctx, shared); !cerrdefs.IsNotFound(err) {
		t.Fatalf("a snapshot of the shared base stayed: %v", err)
	}

	// The tenant's grants read a copy of its pairs of their own.
	store, bucket := storagetest.Client(), layerBucket
	prefix := "agent-test/tenant-" + uuid.NewString()[:8] + "/"
	expires := time.Now().Add(time.Hour)
	tenantConfig, err := tenant.ConfigFile()
	if err != nil {
		t.Fatal(err)
	}
	var grants []*imagefsproto.LayerGrant
	for _, diffID := range tenantConfig.RootFS.DiffIDs {
		grants = append(grants, copyGrant(t, store, bucket, prefix, diffID.String(), expires))
	}
	sources, err := layersource.Dial(layersource.Socket)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sources.Close() }()
	if err := sources.Grant(ctx, "tenant", grants); err != nil {
		t.Fatal(err)
	}
	began := time.Now()
	if _, err := (&imageCache{containerd: ctrd}).ensure(ctx, tenantRef, nil, "linux/amd64"); err != nil {
		t.Fatal(err)
	}
	noLayerSince(t, tenantRef, began)
	if _, err := snapshots.Stat(ctx, shared); err != nil {
		t.Fatalf("the tenant's pull committed no snapshot of the base: %v", err)
	}
	// A read of the base through the tenant's grant now fails.
	for _, g := range grants {
		key := prefix + strings.TrimPrefix(g.GetDiffId(), "sha256:")
		for _, object := range []string{"/index", "/data"} {
			if _, err := store.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(bucket), Key: aws.String(key + object)}); err != nil {
				t.Fatal(err)
			}
		}
	}

	e.startAgent()
	session := e.session()
	start := e.podCommand(&hostproto.PodWorkload{Command: []string{"sleep", "300"}})
	start.GetStart().Checkpointable = true
	id := start.GetStart().GetContainerId()
	session.send(t, start)
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	holder, err := e.docker.ContainerInspect(ctx, "lazycloud-"+id+"-net", client.ContainerInspectOptions{})
	if err != nil || !holder.Container.State.Running || holder.Container.Config.Image != platformimages.Mount {
		t.Fatalf("network holder %+v: %v", holder.Container, err)
	}

	// A restarted agent names the copy its holder runs, so its grants keep
	// coming whatever image the agent now names.
	close(session.end)
	session = e.session()
	if running := session.hello.GetRunningPlatformImages(); len(running) != 1 || !strings.Contains(running[0], "busybox") {
		t.Fatalf("the Hello names the running copies %v", running)
	}
	session.send(t, stopCommand(id, 0))
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
}

// copyGrant copies the pair of diffID that TestMain converted under prefix
// in bucket and returns a grant of the copy until expires.
func copyGrant(t *testing.T, store *s3.Client, bucket, prefix, diffID string, expires time.Time) *imagefsproto.LayerGrant {
	t.Helper()
	key := prefix + strings.TrimPrefix(diffID, "sha256:")
	source := "agent-test/layers/" + strings.TrimPrefix(diffID, "sha256:")
	presign := s3.NewPresignClient(store)
	urls := map[string]string{}
	for _, object := range []string{"index", "data"} {
		if _, err := store.CopyObject(t.Context(), &s3.CopyObjectInput{
			Bucket: aws.String(bucket), Key: aws.String(key + "/" + object), CopySource: aws.String(bucket + "/" + source + "/" + object),
		}); err != nil {
			t.Fatalf("copy the pair of %s: %v", diffID, err)
		}
		signed, err := presign.PresignGetObject(t.Context(), &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(key + "/" + object)},
			s3.WithPresignExpires(time.Until(expires)))
		if err != nil {
			t.Fatal(err)
		}
		urls[object] = signed.URL
	}
	return &imagefsproto.LayerGrant{DiffId: diffID, IndexUrl: urls["index"], DataUrl: urls["data"], ExpiresAt: timestamppb.New(expires)}
}

// withPlatformAnswer makes sessions open with answer's platform images.
func (e *env) withPlatformAnswer(answer func(named []string) []*hostproto.PlatformImage) {
	e.server.mu.Lock()
	defer e.server.mu.Unlock()
	e.server.answerPlatform = answer
}

// A start that fails while its network holder waits for the mount image
// ends at once, not when the image comes.
func TestAFailedStartDoesNotWaitForItsHoldersImage(t *testing.T) {
	t.Parallel()
	e := newEnv(t)
	e.withPlatformAnswer(func([]string) []*hostproto.PlatformImage {
		return []*hostproto.PlatformImage{platformImage(platformimages.Builder)}
	})
	e.startAgent()
	session := e.session()
	start := e.startCommand("app:handle", 1)
	start.GetStart().Checkpointable = true
	start.GetStart().PythonVersion = "2.7"
	session.send(t, start)
	report := session.until(t, 30*time.Second, func(m *hostproto.HostMessage) bool {
		r := m.GetContainer()
		return r.GetContainerId() == start.GetStart().GetContainerId() && r.GetPhase() == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED
	}).GetContainer()
	if report.GetExit().GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED {
		t.Fatalf("the start ended %v", report.GetExit())
	}
}

// A platform image failure one session sent does not fail starts on the
// next: they wait for that session's answer.
func TestAPlatformFailureLastsOnlyItsSession(t *testing.T) {
	t.Parallel()
	e := newEnv(t)
	e.withPlatformAnswer(func([]string) []*hostproto.PlatformImage {
		return []*hostproto.PlatformImage{platformImage(platformimages.Builder), {Reference: platformimages.Mount, Failure: "a layer cannot be converted"}}
	})
	e.startAgent()
	session := e.session()
	failed := e.startCommand("app:handle", 1)
	failed.GetStart().Checkpointable = true
	session.send(t, failed)
	exit := session.phase(t, failed.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if !strings.Contains(exit.GetMessage(), "a layer cannot be converted") {
		t.Fatalf("the failure reaches the start: %v", exit)
	}

	e.withPlatformAnswer(func([]string) []*hostproto.PlatformImage { return nil })
	close(session.end)
	session = e.session()
	start := e.startCommand("app:handle", 1)
	start.GetStart().Checkpointable = true
	id := start.GetStart().GetContainerId()
	session.send(t, start)
	time.Sleep(time.Second)
	session.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_PlatformImages{
		PlatformImages: &hostproto.PlatformImages{Images: []*hostproto.PlatformImage{platformImage(platformimages.Mount)}},
	}})
	report := session.until(t, 120*time.Second, func(m *hostproto.HostMessage) bool {
		r := m.GetContainer()
		return r.GetContainerId() == id && (r.GetPhase() == hostproto.ContainerPhase_CONTAINER_PHASE_READY || r.GetPhase() == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
	}).GetContainer()
	if report.GetPhase() != hostproto.ContainerPhase_CONTAINER_PHASE_READY {
		t.Fatalf("the start failed on the earlier session's failure: %v", report.GetExit())
	}
	session.send(t, stopCommand(id, 0))
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
}

// pushTestImage pushes img to repository and returns it by digest.
func pushTestImage(t *testing.T, repository string, img v1.Image) string {
	t.Helper()
	ref, err := name.ParseReference(repository+":test", name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	if err := remote.Write(ref, img, remote.WithContext(t.Context())); err != nil {
		t.Fatal(err)
	}
	d, err := img.Digest()
	if err != nil {
		t.Fatal(err)
	}
	return repository + "@" + d.String()
}

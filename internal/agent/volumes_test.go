package agent

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"github.com/moby/moby/client"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// testGeeseFS is the pinned GeeseFS binary deploy/local/fetch-geesefs.sh
// installs.
func testGeeseFS(t *testing.T) string {
	t.Helper()
	path, err := filepath.Abs("../../bin/geesefs")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Skipf("volume mounts need GeeseFS at %s; run deploy/local/fetch-geesefs.sh", path)
	}
	return path
}

func volumeStart(e *env, source *hostproto.Source, workspace, volume string, readOnly bool) *hostproto.ServerMessage {
	start := e.startCommand("app:handle", 1)
	start.GetStart().Source = source
	start.GetStart().Volumes = []*hostproto.VolumeMount{{
		MountPath: "/volumes/data", ReadOnly: readOnly,
		Source: &hostproto.VolumeMount_Volume{Volume: &hostproto.PlatformVolume{
			VolumeId: volume, WorkspaceId: workspace, Prefix: "volumes/" + volume + "/",
		}},
	}}
	return start
}

// TestVolumesMountThroughWorkspaceBucket runs two containers on one volume:
// what one writes lands in the workspace bucket under the volume's prefix
// and the other reads it, while neither sees a credential.
func TestVolumesMountThroughWorkspaceBucket(t *testing.T) {
	geesefs := testGeeseFS(t)
	// Made first so that the mounts stop before the bucket goes.
	cfg := storagetest.Config(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	e.startAgent()
	s := e.session()
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()

	start := volumeStart(e, source, workspace, volume, false)
	s.send(t, start)
	// The start waits for the workspace's grant.
	time.Sleep(500 * time.Millisecond)
	s.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_StorageGrant{StorageGrant: &hostproto.StorageGrant{
		WorkspaceId: workspace, Endpoint: cfg.Endpoint, Region: cfg.Region, Bucket: cfg.Bucket,
		AccessKeyId: cfg.AccessKeyID, SecretAccessKey: cfg.SecretAccessKey, ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
	}}})
	writer := start.GetStart().GetContainerId()
	s.phase(t, writer, ready)

	attempt := e.task(writer, `{"args": ["write", "/volumes/data/notes/hello.txt", "hello volume"]}`)
	if got := result(t, e.completion(attempt)); got != "12" {
		t.Fatalf("write: %s", got)
	}
	attempt = e.task(writer, `{"args": ["env", ""]}`)
	if got := result(t, e.completion(attempt)); got != "[]" {
		t.Fatalf("the container sees credentials: %s", got)
	}

	reader := volumeStart(e, source, workspace, volume, true)
	s.send(t, reader)
	s.phase(t, reader.GetStart().GetContainerId(), ready)
	attempt = e.task(reader.GetStart().GetContainerId(), `{"args": ["read", "/volumes/data/notes/hello.txt"]}`)
	if got := result(t, e.completion(attempt)); got != `"hello volume"` {
		t.Fatalf("read: %s", got)
	}
	attempt = e.task(reader.GetStart().GetContainerId(), `{"args": ["write", "/volumes/data/nope.txt", "x"]}`)
	if failure := e.completion(attempt).GetFailure(); failure == nil {
		t.Fatal("a read-only mount accepted a write")
	}

	key := "volumes/" + volume + "/notes/hello.txt"
	object, err := storagetest.Client().GetObject(t.Context(), &s3.GetObjectInput{Bucket: aws.String(cfg.Bucket), Key: aws.String(key)})
	if err != nil {
		t.Fatalf("read %s from the bucket: %v", key, err)
	}
	body, _ := io.ReadAll(object.Body)
	_ = object.Body.Close()
	if string(body) != "hello volume" {
		t.Fatalf("bucket holds %q", body)
	}
	info, err := os.Stat(filepath.Join(e.stateDir, "mounts"))
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("mount directory mode %v err=%v, want 0700", info.Mode().Perm(), err)
	}

	// A mount that dies stops the containers using it.
	timeout := 10
	if _, err := e.docker.ContainerStop(t.Context(), mounterName(workspace), client.ContainerStopOptions{Timeout: &timeout}); err != nil {
		t.Fatal(err)
	}
	// The two exits arrive in either order.
	pending := map[string]bool{writer: true, reader.GetStart().GetContainerId(): true}
	for len(pending) > 0 {
		report := s.until(t, 60*time.Second, func(m *hostproto.HostMessage) bool {
			r := m.GetContainer()
			return pending[r.GetContainerId()] && r.GetPhase() == exited
		}).GetContainer()
		if !strings.Contains(report.GetExit().GetMessage(), "volume mount") {
			t.Fatalf("exit of a container on a dead mount: %v", report.GetExit())
		}
		delete(pending, report.GetContainerId())
	}
}

// TestCloudBucketMountsWithItsKeys mounts a user's bucket with keys from the
// start and removes the mount and its keys when the container goes.
func TestCloudBucketMountsWithItsKeys(t *testing.T) {
	geesefs := testGeeseFS(t)
	cfg := storagetest.Config(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	e.startAgent()
	s := e.session()
	prefix := "test-buckets/" + uuid.NewString() + "/"
	if _, err := storagetest.Client().PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(cfg.Bucket), Key: aws.String(prefix + "weights.txt"), Body: strings.NewReader("from the bucket"),
	}); err != nil {
		t.Fatal(err)
	}

	start := e.startCommand("app:handle", 1)
	start.GetStart().Source = serveSource(t, "testdata/volumes")
	start.GetStart().Volumes = []*hostproto.VolumeMount{{
		MountPath: "/models", ReadOnly: true,
		Source: &hostproto.VolumeMount_CloudBucket{CloudBucket: &hostproto.CloudBucket{
			Bucket: cfg.Bucket, Prefix: prefix, Region: cfg.Region, Endpoint: cfg.Endpoint, ForcePathStyle: true,
			AccessKeyId: cfg.AccessKeyID, SecretAccessKey: cfg.SecretAccessKey,
		}},
	}}
	id := start.GetStart().GetContainerId()
	s.send(t, start)
	s.phase(t, id, ready)
	attempt := e.task(id, `{"args": ["read", "/models/weights.txt"]}`)
	if got := result(t, e.completion(attempt)); got != `"from the bucket"` {
		t.Fatalf("read: %s", got)
	}
	attempt = e.task(id, `{"args": ["env", ""]}`)
	if got := result(t, e.completion(attempt)); got != "[]" {
		t.Fatalf("the container sees credentials: %s", got)
	}

	s.send(t, stopCommand(id, 1))
	s.phase(t, id, exited)
	keys := filepath.Join(e.stateDir, "storage", "buckets", bucketMountName(id, 0))
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(keys); os.IsNotExist(err) {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal("the bucket's keys outlived its container")
}

// stopMounts stops this test's mount containers so GeeseFS unmounts before
// the state directory is removed.
func (e *env) stopMounts() {
	ctx := context.Background()
	list, err := e.docker.ContainerList(ctx, client.ContainerListOptions{All: true, Filters: client.Filters{}.
		Add("label", "lazycloud.agent="+e.id).Add("label", labelKind)})
	if err != nil {
		e.t.Errorf("list mount containers: %v", err)
		return
	}
	timeout := 10
	for _, c := range list.Items {
		if _, err := e.docker.ContainerStop(ctx, c.ID, client.ContainerStopOptions{Timeout: &timeout}); err != nil {
			e.t.Errorf("stop mount container: %v", err)
		}
	}
}

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
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"github.com/moby/moby/client"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// Development object store credentials from compose.yaml.
const (
	testBucket    = "lazycloud"
	testAccessKey = "GK1a2b3c4d5e6f708192a3b4c5"
	testSecretKey = "6c6f63616c2d6c617a79636c6f75642d6465762d7365637265742d6b65792d31" //nolint:gosec // Development key.
)

func testEndpoint() string {
	if endpoint := os.Getenv("LAZYCLOUD_TEST_OBJECT_STORE_ENDPOINT"); endpoint != "" {
		return endpoint
	}
	return "http://127.0.0.1:23900"
}

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
		WorkspaceId: workspace, Endpoint: testEndpoint(), Region: "garage", Bucket: testBucket,
		AccessKeyId: testAccessKey, SecretAccessKey: testSecretKey, ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
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

	store := s3.New(s3.Options{
		Region: "garage", BaseEndpoint: aws.String(testEndpoint()), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(testAccessKey, testSecretKey, ""),
	})
	key := "volumes/" + volume + "/notes/hello.txt"
	object, err := store.GetObject(t.Context(), &s3.GetObjectInput{Bucket: aws.String(testBucket), Key: aws.String(key)})
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

	t.Cleanup(func() {
		ctx := context.Background()
		pages := s3.NewListObjectsV2Paginator(store, &s3.ListObjectsV2Input{Bucket: aws.String(testBucket), Prefix: aws.String("volumes/" + volume + "/")})
		for pages.HasMorePages() {
			page, err := pages.NextPage(ctx)
			if err != nil {
				return
			}
			for _, o := range page.Contents {
				_, _ = store.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(testBucket), Key: o.Key})
			}
		}
	})
}

// TestCloudBucketMountsWithItsKeys mounts a user's bucket with keys from the
// start and removes the mount and its keys when the container goes.
func TestCloudBucketMountsWithItsKeys(t *testing.T) {
	geesefs := testGeeseFS(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	e.startAgent()
	s := e.session()
	prefix := "test-buckets/" + uuid.NewString() + "/"
	store := s3.New(s3.Options{
		Region: "garage", BaseEndpoint: aws.String(testEndpoint()), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(testAccessKey, testSecretKey, ""),
	})
	if _, err := store.PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(testBucket), Key: aws.String(prefix + "weights.txt"), Body: strings.NewReader("from the bucket"),
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = store.DeleteObject(context.Background(), &s3.DeleteObjectInput{Bucket: aws.String(testBucket), Key: aws.String(prefix + "weights.txt")})
	})

	start := e.startCommand("app:handle", 1)
	start.GetStart().Source = serveSource(t, "testdata/volumes")
	start.GetStart().Volumes = []*hostproto.VolumeMount{{
		MountPath: "/models", ReadOnly: true,
		Source: &hostproto.VolumeMount_CloudBucket{CloudBucket: &hostproto.CloudBucket{
			Bucket: testBucket, Prefix: prefix, Region: "garage", Endpoint: testEndpoint(), ForcePathStyle: true,
			AccessKeyId: testAccessKey, SecretAccessKey: testSecretKey,
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

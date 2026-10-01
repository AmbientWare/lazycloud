package agent

import (
	"context"
	"io"
	"os"
	"path/filepath"
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

// stopMounts stops this test's mount containers so GeeseFS unmounts before
// the state directory is removed.
func (e *env) stopMounts() {
	ctx := context.Background()
	list, err := e.docker.ContainerList(ctx, client.ContainerListOptions{All: true, Filters: client.Filters{}.
		Add("label", "lazycloud.agent="+e.id).Add("label", labelKind+"="+kindMount)})
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

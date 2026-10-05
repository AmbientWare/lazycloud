package agent

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/core/images"
	cerrdefs "github.com/containerd/errdefs"
	"github.com/containerd/platforms"
	"github.com/distribution/reference"
	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// lazyImages are the workload images the tests start. Every host reads
// workload images through its snapshotter, so TestMain converts their
// layers into the development object store and grants them, as publishing
// and the server do for a platform image.
var lazyImages = []string{testImage, "python:3.12-alpine"}

// grantLazyImages converts and grants lazyImages for twelve hours.
func grantLazyImages(ctx context.Context) error {
	cfg := storagetest.Config()
	store := s3.New(s3.Options{
		Region: cfg.Region, BaseEndpoint: aws.String(cfg.Endpoint), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(cfg.AccessKeyID, cfg.SecretAccessKey, ""),
	})
	presign := s3.NewPresignClient(store)
	expires := time.Now().Add(12 * time.Hour)
	var grants []layersource.Grant
	for _, ref := range lazyImages {
		r, err := name.ParseReference(ref)
		if err != nil {
			return err
		}
		img, err := remote.Image(r, remote.WithContext(ctx), remote.WithPlatform(v1.Platform{OS: "linux", Architecture: runtime.GOARCH}))
		if err != nil {
			return fmt.Errorf("resolve %s: %w", ref, err)
		}
		layers, err := img.Layers()
		if err != nil {
			return err
		}
		for _, l := range layers {
			grant, err := convertLayer(ctx, store, presign, cfg.Bucket, l, expires)
			if err != nil {
				return fmt.Errorf("convert a layer of %s: %w", ref, err)
			}
			grants = append(grants, grant)
		}
	}
	sources, err := layersource.Dial(layersource.Socket)
	if err != nil {
		return err
	}
	defer func() { _ = sources.Close() }()
	return sources.Grant(ctx, grants)
}

func convertLayer(ctx context.Context, store *s3.Client, presign *s3.PresignClient, bucket string, l v1.Layer, expires time.Time) (layersource.Grant, error) {
	tar, err := l.Uncompressed()
	if err != nil {
		return layersource.Grant{}, err
	}
	defer func() { _ = tar.Close() }()
	data, err := os.CreateTemp("", "layer-data")
	if err != nil {
		return layersource.Grant{}, err
	}
	defer func() { _ = data.Close(); _ = os.Remove(data.Name()) }()
	ix, err := imagefs.Convert(ctx, tar, data)
	if err != nil {
		return layersource.Grant{}, err
	}
	index, err := ix.Marshal()
	if err != nil {
		return layersource.Grant{}, err
	}
	if _, err := data.Seek(0, 0); err != nil {
		return layersource.Grant{}, err
	}
	key := "agent-test/layers/" + strings.TrimPrefix(string(ix.Layer), "sha256:")
	urls := map[string]string{}
	for object, body := range map[string]io.Reader{"index": bytes.NewReader(index), "data": data} {
		if _, err := store.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(bucket), Key: aws.String(key + "/" + object), Body: body}); err != nil {
			return layersource.Grant{}, fmt.Errorf("upload %s: %w", object, err)
		}
		signed, err := presign.PresignGetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(key + "/" + object)},
			s3.WithPresignExpires(time.Until(expires)))
		if err != nil {
			return layersource.Grant{}, err
		}
		urls[object] = signed.URL
	}
	return layersource.Grant{Layer: ix.Layer, IndexURL: urls["index"], DataURL: urls["data"], ExpiresAt: expires}, nil
}

// A converted image pulls through containerd with no layer downloaded and
// runs under Docker from its lazily read layers.
func TestLazyPullDownloadsNoLayer(t *testing.T) {
	ctx := t.Context()
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
	named, err := reference.ParseDockerRef(testImage)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := docker.ImageRemove(ctx, testImage, client.ImageRemoveOptions{Force: true}); err != nil && !cerrdefs.IsNotFound(err) {
		t.Fatal(err)
	}

	began := time.Now()
	pulled, err := cache.ensureLazy(ctx, testImage, nil, "")
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
	// containerd shares content across namespaces, so a blob another pull
	// stored may be present; none may arrive with this one.
	for _, layer := range manifest.Layers {
		info, err := ctrd.ContentStore().Info(ctx, layer.Digest)
		if err == nil && !info.CreatedAt.Before(began) {
			t.Fatalf("the pull downloaded layer blob %s", layer.Digest)
		}
		if err != nil && !cerrdefs.IsNotFound(err) {
			t.Fatal(err)
		}
	}
	if again, err := cache.ensureLazy(ctx, testImage, nil, ""); err != nil || again {
		t.Fatalf("a second pull: pulled %v, %v", again, err)
	}

	began = time.Now()
	created, err := docker.ContainerCreate(ctx, client.ContainerCreateOptions{
		Config: &containertypes.Config{Image: testImage, User: "0",
			Entrypoint: []string{"python", "-c", "import json, sqlite3, asyncio, ssl"}},
		HostConfig: &containertypes.HostConfig{Runtime: testRuntime()},
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		_, _ = docker.ContainerRemove(context.Background(), created.ID, client.ContainerRemoveOptions{Force: true})
	}()
	if _, err := docker.ContainerStart(ctx, created.ID, client.ContainerStartOptions{}); err != nil {
		t.Fatal(err)
	}
	wait := docker.ContainerWait(ctx, created.ID, client.ContainerWaitOptions{})
	select {
	case result := <-wait.Result:
		if result.StatusCode != 0 {
			t.Fatalf("the container exited %d", result.StatusCode)
		}
	case err := <-wait.Error:
		t.Fatal(err)
	}
	t.Logf("run took %v", time.Since(began))
}

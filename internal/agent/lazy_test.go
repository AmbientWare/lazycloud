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
	"github.com/aws/aws-sdk-go-v2/service/s3"
	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/core/images"
	cerrdefs "github.com/containerd/errdefs"
	"github.com/containerd/platforms"
	"github.com/distribution/reference"
	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// lazyImages are the workload and platform images the tests start from a
// registry. Every host reads images through its snapshotter, so TestMain
// converts their layers into layerBucket, as the server does, and start
// commands and sessions carry the grants it would send.
var lazyImages = []string{testImage, "python:3.12-alpine", testDockerImage, platformimages.Builder, platformimages.Mount}

// imageLayers holds the layer grants of each of lazyImages.
var imageLayers = map[string][]*hostproto.LayerGrant{}

// layerBucket is the test store's bucket the test pairs go to, made and
// removed by TestMain.
var layerBucket string //nolint:gochecknoglobals // Set once in TestMain.

// layerStore is the test store and its bucket for pairs.
func layerStore() (*s3.Client, string) { return storagetest.Client(), layerBucket }

// convertLazyImages fills imageLayers with grants for twelve hours.
func convertLazyImages(ctx context.Context) error {
	store, bucket := layerStore()
	presign := s3.NewPresignClient(store)
	expires := time.Now().Add(12 * time.Hour)
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
			grant, err := convertLayer(ctx, store, presign, bucket, "agent-test/layers/", l, expires)
			if err != nil {
				return fmt.Errorf("convert a layer of %s: %w", ref, err)
			}
			imageLayers[ref] = append(imageLayers[ref], grant)
		}
	}
	return nil
}

// convertLayer converts l into a pair under prefix and returns a grant of
// it until expires.
func convertLayer(ctx context.Context, store *s3.Client, presign *s3.PresignClient, bucket, prefix string, l v1.Layer, expires time.Time) (*hostproto.LayerGrant, error) {
	tar, err := l.Uncompressed()
	if err != nil {
		return nil, err
	}
	defer func() { _ = tar.Close() }()
	data, err := os.CreateTemp("", "layer-data")
	if err != nil {
		return nil, err
	}
	defer func() { _ = data.Close(); _ = os.Remove(data.Name()) }()
	ix, err := imagefs.Convert(ctx, tar, data)
	if err != nil {
		return nil, err
	}
	index, err := ix.Marshal()
	if err != nil {
		return nil, err
	}
	if _, err := data.Seek(0, 0); err != nil {
		return nil, err
	}
	key := prefix + strings.TrimPrefix(string(ix.Layer), "sha256:")
	urls := map[string]string{}
	for object, body := range map[string]io.Reader{"index": bytes.NewReader(index), "data": data} {
		if _, err := store.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(bucket), Key: aws.String(key + "/" + object), Body: body}); err != nil {
			return nil, fmt.Errorf("upload %s: %w", object, err)
		}
		signed, err := presign.PresignGetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(key + "/" + object)},
			s3.WithPresignExpires(time.Until(expires)))
		if err != nil {
			return nil, err
		}
		urls[object] = signed.URL
	}
	return &hostproto.LayerGrant{DiffId: string(ix.Layer), IndexUrl: urls["index"], DataUrl: urls["data"], ExpiresAt: timestamppb.New(expires)}, nil
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

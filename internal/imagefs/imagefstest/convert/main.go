// Command convert converts pinned images from the image cache into layer
// pairs in a bucket and prints a grant of every layer, as JSON keyed by
// image with each grant in protojson. Test binaries built with the race
// detector run it to get their image fixtures from a process without the
// instrumentation, which makes conversion many times slower.
//
// Usage: convert -endpoint URL -region NAME -bucket NAME -prefix KEY/ -expires DURATION IMAGE...
//
// AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY name the bucket's key.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefstest"
)

func main() {
	if err := run(context.Background(), os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "convert:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("convert", flag.ContinueOnError)
	endpoint := flags.String("endpoint", "", "the store's URL")
	region := flags.String("region", "", "the store's region")
	bucket := flags.String("bucket", "", "the bucket the pairs go to")
	prefix := flags.String("prefix", "", "the key prefix of the pairs")
	expires := flags.Duration("expires", 0, "how long the grants last")
	if err := flags.Parse(args); err != nil {
		return fmt.Errorf("parse the flags: %w", err)
	}
	if *endpoint == "" || *region == "" || *bucket == "" || *prefix == "" || *expires <= 0 || flags.NArg() == 0 {
		return errors.New("convert needs the store's endpoint, region and bucket, a key prefix, a positive grant duration and an image")
	}
	store := s3.New(s3.Options{
		Region: *region, BaseEndpoint: aws.String(*endpoint), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(os.Getenv("AWS_ACCESS_KEY_ID"), os.Getenv("AWS_SECRET_ACCESS_KEY"), ""),
	})
	until := time.Now().Add(*expires)
	out := map[string][]json.RawMessage{}
	for _, reference := range flags.Args() {
		img, err := imagefstest.Image(ctx, reference, v1.Platform{OS: "linux", Architecture: runtime.GOARCH})
		if err != nil {
			return err
		}
		layers, err := grants(ctx, store, *bucket, *prefix, img, until)
		if err != nil {
			return fmt.Errorf("%s: %w", reference, err)
		}
		for _, grant := range layers {
			encoded, err := protojson.Marshal(grant)
			if err != nil {
				return fmt.Errorf("%s: %w", reference, err)
			}
			out[reference] = append(out[reference], encoded)
		}
	}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		return fmt.Errorf("print the grants: %w", err)
	}
	return nil
}

// grants converts every layer of img into a pair under prefix in bucket,
// as the server converts images, and returns a grant of each until
// expires, in the image's layer order.
func grants(ctx context.Context, store *s3.Client, bucket, prefix string, img v1.Image, expires time.Time) ([]*imagefsproto.LayerGrant, error) {
	layers, err := img.Layers()
	if err != nil {
		return nil, fmt.Errorf("read the image's layers: %w", err)
	}
	presign := s3.NewPresignClient(store)
	grants := make([]*imagefsproto.LayerGrant, 0, len(layers))
	for _, l := range layers {
		grant, err := grantLayer(ctx, store, presign, bucket, prefix, l, expires)
		if err != nil {
			return nil, err
		}
		grants = append(grants, grant)
	}
	return grants, nil
}

func grantLayer(ctx context.Context, store *s3.Client, presign *s3.PresignClient, bucket, prefix string, l v1.Layer, expires time.Time) (*imagefsproto.LayerGrant, error) {
	tar, err := l.Uncompressed()
	if err != nil {
		return nil, fmt.Errorf("read a layer: %w", err)
	}
	defer func() { _ = tar.Close() }()
	data, err := os.CreateTemp("", "layer-data")
	if err != nil {
		return nil, fmt.Errorf("convert a layer: %w", err)
	}
	defer func() { _ = data.Close(); _ = os.Remove(data.Name()) }()
	ix, err := imagefs.Convert(ctx, tar, data)
	if err != nil {
		return nil, fmt.Errorf("convert a layer: %w", err)
	}
	index, err := ix.Marshal()
	if err != nil {
		return nil, fmt.Errorf("convert a layer: %w", err)
	}
	if _, err := data.Seek(0, io.SeekStart); err != nil {
		return nil, fmt.Errorf("convert a layer: %w", err)
	}
	key := prefix + strings.TrimPrefix(string(ix.Layer), "sha256:")
	urls := map[string]string{}
	for object, body := range map[string]io.Reader{"index": bytes.NewReader(index), "data": data} {
		if _, err := store.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(bucket), Key: aws.String(key + "/" + object), Body: body}); err != nil {
			return nil, fmt.Errorf("upload %s of %s: %w", object, ix.Layer, err)
		}
		signed, err := presign.PresignGetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(key + "/" + object)},
			s3.WithPresignExpires(time.Until(expires)))
		if err != nil {
			return nil, fmt.Errorf("presign %s of %s: %w", object, ix.Layer, err)
		}
		urls[object] = signed.URL
	}
	return &imagefsproto.LayerGrant{DiffId: string(ix.Layer), IndexUrl: urls["index"], DataUrl: urls["data"], ExpiresAt: timestamppb.New(expires)}, nil
}

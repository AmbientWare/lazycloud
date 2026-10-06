// Package storagetest gives tests buckets of their own in the test Garage
// from compose.test.yaml and deletes them, with every object in them and
// every key granted on them, when the tests end.
package storagetest

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/smithy-go"

	"github.com/AmbientWare/lazycloud/internal/storage"
)

// The test Garage's region, and the development key and admin token its
// bootstrap sets up.
const (
	region          = "garage"
	accessKeyID     = "GK1a2b3c4d5e6f708192a3b4c5"
	secretAccessKey = "6c6f63616c2d6c617a79636c6f75642d6465762d7365637265742d6b65792d31" //nolint:gosec // Development key.
	adminToken      = "local-garage-admin"                                               //nolint:gosec // Development token.
	// namePrefix starts every test bucket's name.
	namePrefix = "lazycloud-test-"
	// staleAfter outlasts any test binary. Open removes test buckets older
	// than that, left by a binary that panicked or timed out before its
	// cleanup ran.
	staleAfter = 24 * time.Hour
)

// Config returns buckets made for t alone: a platform bucket, a layer bucket
// and a workspace bucket prefix, all named after one random prefix.
func Config(t testing.TB) storage.Config {
	t.Helper()
	cfg, remove, err := Open(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := remove(context.Background()); err != nil {
			t.Error(err)
		}
	})
	return cfg
}

// Open creates buckets shared by whatever uses cfg, such as every test of a
// binary whose TestMain calls it. remove deletes them, with the workspace
// buckets made under cfg's prefix, every object in them and the keys
// granted on them.
func Open(ctx context.Context) (cfg storage.Config, remove func(context.Context) error, err error) {
	suffix := make([]byte, 4)
	_, _ = rand.Read(suffix)
	// Workspace buckets append a dash and 32 hex digits, within S3's 63.
	prefix := namePrefix + hex.EncodeToString(suffix)
	admin := garage{url: strings.TrimRight(adminURL(), "/"), http: &http.Client{Timeout: 30 * time.Second}}
	stale := time.Now().Add(-staleAfter)
	if err := admin.removeBuckets(ctx, func(alias string, created time.Time) bool {
		return strings.HasPrefix(alias, namePrefix) && created.Before(stale)
	}); err != nil {
		return storage.Config{}, nil, fmt.Errorf("test object store (docker compose -f compose.test.yaml up -d --wait): %w", err)
	}
	cfg = storage.Config{
		Endpoint:        endpoint(),
		Region:          region,
		Bucket:          prefix,
		LayerBucket:     prefix + "-layers",
		AccessKeyID:     accessKeyID,
		SecretAccessKey: secretAccessKey,
		Workspaces: storage.WorkspaceBuckets{
			Provider:         storage.ProviderGarage,
			Prefix:           prefix,
			GarageAdminURL:   admin.url,
			GarageAdminToken: adminToken,
		},
	}
	remove = func(ctx context.Context) error {
		return admin.removeBuckets(ctx, func(alias string, _ time.Time) bool {
			return alias == prefix || strings.HasPrefix(alias, prefix+"-")
		})
	}
	for _, bucket := range []string{cfg.Bucket, cfg.LayerBucket} {
		if err := admin.createBucket(ctx, bucket); err != nil {
			return storage.Config{}, nil, errors.Join(
				fmt.Errorf("test object store (docker compose -f compose.test.yaml up -d --wait): %w", err),
				remove(context.WithoutCancel(ctx)))
		}
	}
	return cfg, remove, nil
}

// Client is an S3 client on the test Garage with the development key.
func Client() *s3.Client {
	return s3.New(s3.Options{
		Region: region, BaseEndpoint: aws.String(endpoint()), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(accessKeyID, secretAccessKey, ""),
	})
}

// endpoint is the test Garage's S3 API and adminURL its admin API.
// LAZYCLOUD_TEST_OBJECT_STORE_ENDPOINT and LAZYCLOUD_TEST_GARAGE_ADMIN_URL
// name another Garage with the same development key and admin token.
func endpoint() string {
	if endpoint := os.Getenv("LAZYCLOUD_TEST_OBJECT_STORE_ENDPOINT"); endpoint != "" {
		return endpoint
	}
	return "http://127.0.0.1:15900"
}

func adminURL() string {
	if admin := os.Getenv("LAZYCLOUD_TEST_GARAGE_ADMIN_URL"); admin != "" {
		return admin
	}
	return "http://127.0.0.1:15903"
}

// garage calls the Garage admin API.
type garage struct {
	url  string
	http *http.Client
}

func (g garage) call(ctx context.Context, method, op string, query url.Values, body, out any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode garage %s: %w", op, err)
		}
		reader = bytes.NewReader(encoded)
	}
	target := g.url + "/v2/" + op
	if len(query) > 0 {
		target += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, target, reader)
	if err != nil {
		return fmt.Errorf("garage %s: %w", op, err)
	}
	req.Header.Set("Authorization", "Bearer "+adminToken)
	req.Header.Set("Content-Type", "application/json")
	resp, err := g.http.Do(req)
	if err != nil {
		return fmt.Errorf("garage %s: %w", op, err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		return fmt.Errorf("garage %s: read response: %w", op, err)
	}
	if resp.StatusCode == http.StatusNotFound {
		return notFoundError{op: op}
	}
	if resp.StatusCode >= 300 {
		return fmt.Errorf("garage %s: HTTP %d: %s", op, resp.StatusCode, strings.TrimSpace(string(data)))
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			return fmt.Errorf("garage %s: decode response: %w", op, err)
		}
	}
	return nil
}

// createBucket creates bucket and lets the development key own it.
func (g garage) createBucket(ctx context.Context, bucket string) error {
	var created struct {
		ID string `json:"id"`
	}
	if err := g.call(ctx, http.MethodPost, "CreateBucket", nil, map[string]string{"globalAlias": bucket}, &created); err != nil {
		return err
	}
	return g.call(ctx, http.MethodPost, "AllowBucketKey", nil, map[string]any{
		"bucketId": created.ID, "accessKeyId": accessKeyID,
		"permissions": map[string]bool{"read": true, "write": true, "owner": true},
	}, nil)
}

// removeBuckets removes every bucket whose alias and creation time match,
// reporting every failure.
func (g garage) removeBuckets(ctx context.Context, match func(alias string, created time.Time) bool) error {
	var buckets []struct {
		ID            string    `json:"id"`
		Created       time.Time `json:"created"`
		GlobalAliases []string  `json:"globalAliases"`
	}
	if err := g.call(ctx, http.MethodGet, "ListBuckets", nil, nil, &buckets); err != nil {
		return err
	}
	objects := Client()
	var errs []error
	for _, bucket := range buckets {
		for _, alias := range bucket.GlobalAliases {
			if match(alias, bucket.Created) {
				if err := g.removeBucket(ctx, objects, bucket.ID, alias); err != nil {
					errs = append(errs, fmt.Errorf("remove bucket %s: %w", alias, err))
				}
				break
			}
		}
	}
	return errors.Join(errs...)
}

// removeBucket deletes the keys granted on a bucket other than the
// development key, then its objects and the bucket. What another caller
// removed first counts as removed.
func (g garage) removeBucket(ctx context.Context, objects *s3.Client, id, alias string) error {
	var info struct {
		Keys []struct {
			AccessKeyID string `json:"accessKeyId"`
		} `json:"keys"`
	}
	if err := g.call(ctx, http.MethodGet, "GetBucketInfo", url.Values{"id": {id}}, nil, &info); err != nil {
		return present(err)
	}
	var errs []error
	for _, key := range info.Keys {
		if key.AccessKeyID == accessKeyID {
			continue
		}
		if err := g.call(ctx, http.MethodPost, "DeleteKey", url.Values{"id": {key.AccessKeyID}}, nil, nil); present(err) != nil {
			errs = append(errs, err)
		}
	}
	if err := empty(ctx, objects, alias); present(err) != nil {
		return errors.Join(append(errs, err)...)
	}
	if err := g.call(ctx, http.MethodPost, "DeleteBucket", url.Values{"id": {id}}, nil, nil); present(err) != nil {
		errs = append(errs, err)
	}
	return errors.Join(errs...)
}

// notFoundError is the admin API's answer for a bucket or key that is gone.
type notFoundError struct{ op string }

func (e notFoundError) Error() string { return "garage " + e.op + ": not found" }

// present is err, or nil when err says the bucket or key is already gone.
func present(err error) error {
	var missing notFoundError
	var api smithy.APIError
	if errors.As(err, &missing) || errors.As(err, &api) && api.ErrorCode() == "NoSuchBucket" {
		return nil
	}
	return err
}

// empty deletes every object in bucket and aborts its multipart uploads.
func empty(ctx context.Context, objects *s3.Client, bucket string) error {
	listing := s3.NewListObjectsV2Paginator(objects, &s3.ListObjectsV2Input{Bucket: aws.String(bucket)})
	for listing.HasMorePages() {
		page, err := listing.NextPage(ctx)
		if err != nil {
			return fmt.Errorf("list %s: %w", bucket, err)
		}
		if len(page.Contents) == 0 {
			continue
		}
		ids := make([]s3types.ObjectIdentifier, 0, len(page.Contents))
		for _, object := range page.Contents {
			ids = append(ids, s3types.ObjectIdentifier{Key: object.Key})
		}
		out, err := objects.DeleteObjects(ctx, &s3.DeleteObjectsInput{
			Bucket: aws.String(bucket), Delete: &s3types.Delete{Objects: ids, Quiet: aws.Bool(true)},
		})
		if err != nil {
			return fmt.Errorf("delete objects in %s: %w", bucket, err)
		}
		if len(out.Errors) > 0 {
			return fmt.Errorf("delete %s in %s: %s", aws.ToString(out.Errors[0].Key), bucket, aws.ToString(out.Errors[0].Message))
		}
	}
	uploads := s3.NewListMultipartUploadsPaginator(objects, &s3.ListMultipartUploadsInput{Bucket: aws.String(bucket)})
	for uploads.HasMorePages() {
		page, err := uploads.NextPage(ctx)
		if err != nil {
			return fmt.Errorf("list uploads in %s: %w", bucket, err)
		}
		for _, upload := range page.Uploads {
			if _, err := objects.AbortMultipartUpload(ctx, &s3.AbortMultipartUploadInput{
				Bucket: aws.String(bucket), Key: upload.Key, UploadId: upload.UploadId,
			}); err != nil {
				return fmt.Errorf("abort an upload in %s: %w", bucket, err)
			}
		}
	}
	return nil
}

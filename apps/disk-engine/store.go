package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	smithyhttp "github.com/aws/smithy-go/transport/http"
	"golang.org/x/sync/errgroup"
)

// storeConfig is STORE.json: the workspace bucket credentials the worker holds.
type storeConfig struct {
	EndpointURL    string     `json:"endpoint_url"`
	Region         string     `json:"region"`
	Bucket         string     `json:"bucket"`
	AccessKey      string     `json:"access_key"`
	SecretKey      string     `json:"secret_key"`
	SessionToken   string     `json:"session_token"`
	ForcePathStyle bool       `json:"force_path_style"`
	ExpiresAt      *time.Time `json:"expires_at"`
}

// storeCredentialMargin is how long before its credentials expire the engine
// reads STORE.json again. The worker replaces the file at half the grant's
// life, so a fresh grant is normally waiting there well before this.
const storeCredentialMargin = 2 * time.Minute

// storeCredentials signs with the credentials in STORE.json and reads the file
// again once they come within storeCredentialMargin of expiring. One engine
// call can outlast one grant; the worker rewrites the file while the call runs.
type storeCredentials struct {
	path    string
	now     func() time.Time
	mu      sync.Mutex
	current aws.Credentials
}

func (p *storeCredentials) Retrieve(context.Context) (aws.Credentials, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	now := p.now()
	if p.current.HasKeys() &&
		(!p.current.CanExpire || now.Before(p.current.Expires.Add(-storeCredentialMargin))) {
		return p.current, nil
	}
	var config storeConfig
	if err := readJSONFile(p.path, &config); err != nil {
		return aws.Credentials{}, err
	}
	if config.AccessKey == "" || config.SecretKey == "" {
		return aws.Credentials{}, fmt.Errorf("%s has no access_key or secret_key", p.path)
	}
	credentials := aws.Credentials{
		AccessKeyID:     config.AccessKey,
		SecretAccessKey: config.SecretKey,
		SessionToken:    config.SessionToken,
	}
	if config.ExpiresAt != nil {
		if !now.Before(*config.ExpiresAt) {
			return aws.Credentials{}, fmt.Errorf(
				"the workspace storage credentials in %s expired at %s and the worker did not replace them",
				p.path, config.ExpiresAt.UTC().Format(time.RFC3339))
		}
		credentials.CanExpire = true
		credentials.Expires = *config.ExpiresAt
	}
	p.current = credentials
	return credentials, nil
}

type objectStore struct {
	client *s3.Client
	bucket string
}

func openStore(path string) (*objectStore, error) {
	var config storeConfig
	if err := readJSONFile(path, &config); err != nil {
		return nil, err
	}
	for _, field := range [][2]string{{"region", config.Region}, {"bucket", config.Bucket}} {
		if field[1] == "" {
			return nil, fmt.Errorf("%s has no %s", path, field[0])
		}
	}
	credentials := &storeCredentials{path: path, now: time.Now}
	if _, err := credentials.Retrieve(context.Background()); err != nil {
		return nil, err
	}
	options := s3.Options{
		Region:       config.Region,
		Credentials:  credentials,
		UsePathStyle: config.ForcePathStyle,
		// Compute checksums only where S3 requires them. S3-compatible stores
		// reject the streaming trailers the SDK otherwise adds to every upload.
		RequestChecksumCalculation: aws.RequestChecksumCalculationWhenRequired,
		ResponseChecksumValidation: aws.ResponseChecksumValidationWhenRequired,
		RetryMaxAttempts:           5,
	}
	if config.EndpointURL != "" {
		options.BaseEndpoint = aws.String(config.EndpointURL)
	}
	return &objectStore{client: s3.New(options), bucket: config.Bucket}, nil
}

func (s *objectStore) exists(ctx context.Context, key string) (bool, error) {
	_, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: &s.bucket, Key: &key})
	if err == nil {
		return true, nil
	}
	var response *smithyhttp.ResponseError
	if errors.As(err, &response) && response.HTTPStatusCode() == http.StatusNotFound {
		return false, nil
	}
	return false, fmt.Errorf("head s3://%s/%s: %w", s.bucket, key, err)
}

func (s *objectStore) put(ctx context.Context, key string, body []byte, contentType string) error {
	_, err := s.client.PutObject(ctx, &s3.PutObjectInput{
		Bucket:        &s.bucket,
		Key:           &key,
		Body:          bytes.NewReader(body),
		ContentLength: aws.Int64(int64(len(body))),
		ContentType:   aws.String(contentType),
	})
	if err != nil {
		return fmt.Errorf("put s3://%s/%s: %w", s.bucket, key, err)
	}
	return nil
}

func (s *objectStore) get(ctx context.Context, key string, limit int64) ([]byte, error) {
	output, err := s.client.GetObject(ctx, &s3.GetObjectInput{Bucket: &s.bucket, Key: &key})
	if err != nil {
		return nil, fmt.Errorf("get s3://%s/%s: %w", s.bucket, key, err)
	}
	defer output.Body.Close()
	data, err := io.ReadAll(io.LimitReader(output.Body, limit+1))
	if err != nil {
		return nil, fmt.Errorf("read s3://%s/%s: %w", s.bucket, key, err)
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("s3://%s/%s is larger than %d bytes", s.bucket, key, limit)
	}
	return data, nil
}

// getIfExists is get for an object that may never have been written.
func (s *objectStore) getIfExists(ctx context.Context, key string, limit int64) ([]byte, bool, error) {
	data, err := s.get(ctx, key, limit)
	var missing *types.NoSuchKey
	var response *smithyhttp.ResponseError
	if errors.As(err, &missing) ||
		(errors.As(err, &response) && response.HTTPStatusCode() == http.StatusNotFound) {
		return nil, false, nil
	}
	return data, err == nil, err
}

type storedObject struct {
	Key  string
	Size int64
}

func (s *objectStore) list(ctx context.Context, prefix string, visit func(storedObject) error) error {
	pages := s3.NewListObjectsV2Paginator(s.client, &s3.ListObjectsV2Input{Bucket: &s.bucket, Prefix: &prefix})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return fmt.Errorf("list s3://%s/%s: %w", s.bucket, prefix, err)
		}
		for _, object := range page.Contents {
			if err := visit(storedObject{Key: aws.ToString(object.Key), Size: aws.ToInt64(object.Size)}); err != nil {
				return err
			}
		}
	}
	return nil
}

// deleteBatchSize is the most keys one DeleteObjects request accepts.
const deleteBatchSize = 1000

func (s *objectStore) deleteKeys(ctx context.Context, keys []string) error {
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(4)
	for batch := range slices.Chunk(keys, deleteBatchSize) {
		group.Go(func() error {
			objects := make([]types.ObjectIdentifier, len(batch))
			for i, key := range batch {
				objects[i] = types.ObjectIdentifier{Key: aws.String(key)}
			}
			output, err := s.client.DeleteObjects(groupCtx, &s3.DeleteObjectsInput{
				Bucket: &s.bucket,
				Delete: &types.Delete{Objects: objects, Quiet: aws.Bool(true)},
			})
			if err != nil {
				return fmt.Errorf("delete %d objects from s3://%s: %w", len(batch), s.bucket, err)
			}
			if len(output.Errors) > 0 {
				first := output.Errors[0]
				return fmt.Errorf("delete s3://%s/%s: %s: %s (and %d more failures)", s.bucket,
					aws.ToString(first.Key), aws.ToString(first.Code), aws.ToString(first.Message), len(output.Errors)-1)
			}
			return nil
		})
	}
	return group.Wait()
}

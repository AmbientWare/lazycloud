package diskengine

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	smithyhttp "github.com/aws/smithy-go/transport/http"
	"golang.org/x/sync/errgroup"
)

// Credentials sign requests to a workspace bucket. A zero ExpiresAt never
// expires.
type Credentials struct {
	AccessKeyID     string
	SecretAccessKey string
	SessionToken    string
	ExpiresAt       time.Time
}

// Store locates a workspace bucket. Object keys are Prefix +
// "disks/<disk id>/...", so a non-empty Prefix ends with "/". Endpoint is
// empty for AWS S3.
type Store struct {
	Endpoint       string
	Region         string
	Bucket         string
	Prefix         string
	ForcePathStyle bool
	// Credentials returns current credentials. The engine asks again once the
	// ones it holds come within credentialMargin of expiring, so one long
	// upload can outlast one grant.
	Credentials func(ctx context.Context) (Credentials, error)
}

// credentialMargin is how long before its credentials expire the engine asks
// for new ones.
const credentialMargin = 2 * time.Minute

// transferConcurrency bounds the chunk requests one layer has in flight.
const transferConcurrency = 8

// deleteBatchSize is the most keys one DeleteObjects request accepts.
const deleteBatchSize = 1000

type objectStore struct {
	client *s3.Client
	bucket string
	prefix string
}

func openStore(store Store) (*objectStore, error) {
	if store.Region == "" || store.Bucket == "" || store.Credentials == nil {
		return nil, fmt.Errorf("%w: a store needs a region, a bucket and credentials", ErrInvalid)
	}
	fetch := store.Credentials
	provider := aws.CredentialsProviderFunc(func(ctx context.Context) (aws.Credentials, error) {
		current, err := fetch(ctx)
		if err != nil {
			return aws.Credentials{}, fmt.Errorf("storage credentials: %w", err)
		}
		if current.AccessKeyID == "" || current.SecretAccessKey == "" {
			return aws.Credentials{}, errors.New("storage credentials have no access key or secret")
		}
		credentials := aws.Credentials{
			AccessKeyID:     current.AccessKeyID,
			SecretAccessKey: current.SecretAccessKey,
			SessionToken:    current.SessionToken,
		}
		if !current.ExpiresAt.IsZero() {
			if !time.Now().Before(current.ExpiresAt) {
				return aws.Credentials{}, fmt.Errorf("%w at %s and were not replaced",
					ErrCredentialsExpired, current.ExpiresAt.UTC().Format(time.RFC3339))
			}
			credentials.CanExpire = true
			credentials.Expires = current.ExpiresAt
		}
		return credentials, nil
	})
	options := s3.Options{
		Region: store.Region,
		Credentials: aws.NewCredentialsCache(provider, func(o *aws.CredentialsCacheOptions) {
			o.ExpiryWindow = credentialMargin
		}),
		UsePathStyle: store.ForcePathStyle,
		// Compute checksums only where S3 requires them. S3-compatible stores
		// reject the streaming trailers the SDK otherwise adds to every upload.
		RequestChecksumCalculation: aws.RequestChecksumCalculationWhenRequired,
		ResponseChecksumValidation: aws.ResponseChecksumValidationWhenRequired,
		RetryMaxAttempts:           5,
	}
	if store.Endpoint != "" {
		options.BaseEndpoint = aws.String(store.Endpoint)
	}
	return &objectStore{client: s3.New(options), bucket: store.Bucket, prefix: store.Prefix}, nil
}

func (s *objectStore) diskPrefix(diskID string) string { return s.prefix + "disks/" + diskID + "/" }

func (s *objectStore) chunkKey(diskID, sum string) string {
	return s.diskPrefix(diskID) + "chunks/" + sum[:2] + "/" + sum
}

func (s *objectStore) manifestKey(diskID string, generation int64) string {
	return fmt.Sprintf("%smanifests/%012d.json", s.diskPrefix(diskID), generation)
}

func notFound(err error) bool {
	var missing *types.NoSuchKey
	var response *smithyhttp.ResponseError
	return errors.As(err, &missing) ||
		(errors.As(err, &response) && response.HTTPStatusCode() == http.StatusNotFound)
}

func (s *objectStore) exists(ctx context.Context, key string) (bool, error) {
	_, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: &s.bucket, Key: &key})
	if err == nil {
		return true, nil
	}
	if notFound(err) {
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

// get reads an object of at most limit bytes.
func (s *objectStore) get(ctx context.Context, key string, limit int64) ([]byte, error) {
	output, err := s.client.GetObject(ctx, &s3.GetObjectInput{Bucket: &s.bucket, Key: &key})
	if err != nil {
		return nil, fmt.Errorf("get s3://%s/%s: %w", s.bucket, key, err)
	}
	data, err := io.ReadAll(io.LimitReader(output.Body, limit+1))
	closeErr := output.Body.Close()
	if err != nil {
		return nil, fmt.Errorf("read s3://%s/%s: %w", s.bucket, key, err)
	}
	if closeErr != nil {
		return nil, fmt.Errorf("read s3://%s/%s: %w", s.bucket, key, closeErr)
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("s3://%s/%s is larger than %d bytes", s.bucket, key, limit)
	}
	return data, nil
}

type storedObject struct {
	Key  string
	Size int64
}

func (s *objectStore) list(ctx context.Context, prefix string, visit func(storedObject)) error {
	pages := s3.NewListObjectsV2Paginator(s.client, &s3.ListObjectsV2Input{Bucket: &s.bucket, Prefix: &prefix})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return fmt.Errorf("list s3://%s/%s: %w", s.bucket, prefix, err)
		}
		for _, object := range page.Contents {
			visit(storedObject{Key: aws.ToString(object.Key), Size: aws.ToInt64(object.Size)})
		}
	}
	return nil
}

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
	if err := group.Wait(); err != nil {
		return fmt.Errorf("delete objects: %w", err)
	}
	return nil
}

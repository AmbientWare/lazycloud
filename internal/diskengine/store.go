package diskengine

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// Credentials sign requests to a workspace bucket. A zero ExpiresAt never
// expires.
type Credentials struct {
	AccessKeyID     string
	SecretAccessKey string
	SessionToken    string
	ExpiresAt       time.Time
}

// Store locates a workspace bucket, which holds each disk's objects under
// imagefs.DiskPrefix.
type Store struct {
	Endpoint       string
	Region         string
	Bucket         string
	ForcePathStyle bool
	// Credentials returns current credentials. The engine asks again once the
	// ones it holds come within credentialMargin of expiring, so one long
	// upload can outlast one grant.
	Credentials func(ctx context.Context) (Credentials, error)
}

// credentialMargin is how long before its credentials expire the engine asks
// for new ones.
const credentialMargin = 2 * time.Minute

// deleteBatchSize is the most keys one DeleteObjects request accepts, and so
// the most one Remover call is given.
const deleteBatchSize = 1000

type objectStore struct {
	client *s3.Client
	bucket string
}

func openStore(store Store) (*objectStore, error) {
	if store.Endpoint == "" || store.Region == "" || store.Bucket == "" || store.Credentials == nil {
		return nil, fmt.Errorf("%w: a store needs an endpoint, a region, a bucket and credentials", ErrInvalid)
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
		BaseEndpoint:               aws.String(store.Endpoint),
	}
	return &objectStore{client: s3.New(options), bucket: store.Bucket}, nil
}

func (s *objectStore) put(ctx context.Context, key string, body []byte) error {
	_, err := s.client.PutObject(ctx, &s3.PutObjectInput{
		Bucket:        &s.bucket,
		Key:           &key,
		Body:          bytes.NewReader(body),
		ContentLength: aws.Int64(int64(len(body))),
		ContentType:   aws.String("application/octet-stream"),
	})
	if err != nil {
		return fmt.Errorf("put s3://%s/%s: %w", s.bucket, key, err)
	}
	return nil
}

package diskengine

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
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

func openStore(store Store) (imagefs.Bucket, error) {
	if store.Endpoint == "" || store.Region == "" || store.Bucket == "" || store.Credentials == nil {
		return imagefs.Bucket{}, fmt.Errorf("%w: a store needs an endpoint, a region, a bucket and credentials", ErrInvalid)
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
				return aws.Credentials{}, fmt.Errorf("storage credentials expired at %s and were not replaced",
					current.ExpiresAt.UTC().Format(time.RFC3339))
			}
			credentials.CanExpire = true
			credentials.Expires = current.ExpiresAt
		}
		return credentials, nil
	})
	cache := aws.NewCredentialsCache(provider, func(o *aws.CredentialsCacheOptions) { o.ExpiryWindow = credentialMargin })
	return imagefs.NewBucket(store.Endpoint, store.Region, store.Bucket, store.ForcePathStyle, cache,
		func(o *s3.Options) { o.RetryMaxAttempts = 5 }), nil
}

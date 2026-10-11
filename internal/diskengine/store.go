package diskengine

import (
	"context"
	"fmt"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

// Store locates a workspace bucket, which holds each disk's objects under
// imagefs.DiskPrefix.
type Store struct {
	Endpoint       string
	Region         string
	Bucket         string
	ForcePathStyle bool
	// Credentials gives current credentials. The engine asks again once the
	// ones it holds come within credentialMargin of expiring, so one long
	// upload can outlast one grant.
	Credentials aws.CredentialsProvider
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
	current := aws.CredentialsProviderFunc(func(ctx context.Context) (aws.Credentials, error) {
		creds, err := store.Credentials.Retrieve(ctx)
		if err != nil {
			return aws.Credentials{}, fmt.Errorf("storage credentials: %w", err)
		}
		if creds.Expired() {
			return aws.Credentials{}, fmt.Errorf("storage credentials expired at %s and were not replaced", creds.Expires.UTC().Format(time.RFC3339))
		}
		return creds, nil
	})
	cache := aws.NewCredentialsCache(current, func(o *aws.CredentialsCacheOptions) { o.ExpiryWindow = credentialMargin })
	return imagefs.NewBucket(store.Endpoint, store.Region, store.Bucket, store.ForcePathStyle, cache,
		func(o *s3.Options) { o.RetryMaxAttempts = 5 }), nil
}

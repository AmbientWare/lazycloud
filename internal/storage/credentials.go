package storage

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
)

// Validate checks that cfg names an object store the server can use. The
// static key pair is optional: without it the AWS default credential chain
// supplies credentials, such as EKS Pod Identity or IRSA. Garage has no
// such chain and needs the pair, which is also the key its workspace
// buckets are opened to.
func (c Config) Validate() error {
	if c.Region == "" || c.Bucket == "" {
		return errors.New("the object store region and bucket are required")
	}
	if (c.AccessKeyID == "") != (c.SecretAccessKey == "") {
		return errors.New("set both the object store access key id and secret access key, or neither to use the AWS default credential chain")
	}
	if c.Workspaces.Provider == ProviderGarage && c.AccessKeyID == "" {
		return errors.New("a Garage object store needs the static access key id and secret access key")
	}
	if c.Workspaces.Provider == ProviderAWS && c.Workspaces.RoleARN == "" {
		return errors.New("AWS workspace buckets need the role host credentials are issued for")
	}
	return nil
}

// credentialProvider is the static key pair when cfg has one, and
// otherwise the AWS default credential chain: environment, shared config,
// web identity (IRSA), container credentials (EKS Pod Identity, ECS) and
// instance metadata, as aws-sdk-go-v2's config package resolves them.
func credentialProvider(cfg Config) aws.CredentialsProvider {
	if cfg.AccessKeyID != "" {
		return credentials.NewStaticCredentialsProvider(cfg.AccessKeyID, cfg.SecretAccessKey, "")
	}
	return &defaultChain{region: cfg.Region}
}

// credentialWindow is how long before they expire the default chain's
// credentials are renewed. Its cache reports them expiring that much early,
// so a URL they sign may outlive the reported expiry by up to the window
// (signedLifetime).
const credentialWindow = 15 * time.Minute

func renewEarly(o *aws.CredentialsCacheOptions) { o.ExpiryWindow = credentialWindow }

// defaultChain resolves the default credential chain, cached and renewed
// credentialWindow early, on first use, so constructing the owner needs no
// network and a server starts even while the chain's source is slow to
// appear.
type defaultChain struct {
	region string
	once   sync.Once
	chain  aws.CredentialsProvider
	err    error
}

// Retrieve returns credentials from the default chain.
func (d *defaultChain) Retrieve(ctx context.Context) (aws.Credentials, error) {
	d.once.Do(func() {
		loaded, err := config.LoadDefaultConfig(ctx, config.WithRegion(d.region), config.WithCredentialsCacheOptions(renewEarly))
		if err != nil {
			d.err = fmt.Errorf("load the AWS default credential chain: %w", err)
			return
		}
		if loaded.Credentials == nil {
			d.err = errors.New("the AWS default credential chain found no credentials")
			return
		}
		d.chain = loaded.Credentials
	})
	if d.err != nil {
		return aws.Credentials{}, d.err
	}
	creds, err := d.chain.Retrieve(ctx)
	if err != nil {
		return aws.Credentials{}, fmt.Errorf("retrieve credentials from the AWS default chain: %w", err)
	}
	return creds, nil
}

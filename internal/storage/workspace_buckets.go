package storage

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"net/http"
	"slices"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsmiddleware "github.com/aws/aws-sdk-go-v2/aws/middleware"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/smithy-go/middleware"
	smithyhttp "github.com/aws/smithy-go/transport/http"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	// maxPrefix is the longest bucket name prefix: with a hyphen, a
	// 12-digit account id, a hyphen and workspaceDigits, a name fills S3's
	// 63 characters.
	maxPrefix = 24
	// workspaceDigits is how many base-36 digits hold a 128-bit id.
	workspaceDigits = 25
)

// bucketName is <prefix>-<account>-<workspace id in base 36>. S3 bucket
// names are global, and the account id keeps anyone else from creating a
// workspace's name before the platform does.
func bucketName(prefix, account string, workspace identity.WorkspaceID) string {
	id := uuid.UUID(workspace)
	digits := new(big.Int).SetBytes(id[:]).Text(36)
	return prefix + "-" + account + "-" + strings.Repeat("0", workspaceDigits-len(digits)) + digits
}

// bucketClient is a bucket, the region it is in, the connected account
// that holds it, nil for the platform's, and the client and presigner
// signed for that account and region.
type bucketClient struct {
	name       string
	region     string
	connection *uuid.UUID
	client     *s3.Client
	presign    *s3.PresignClient
}

// storeOf is the bucket named name in region of the platform's account, or
// connection's. Clients outside the platform store's own account and region
// are made on first use and kept by account and region. A connection's
// client names the connected account as the owner of every bucket it
// reaches, and assumes the connection's active role whenever its
// credentials renew, so a reconnect's new role takes over within the hour.
func (s *Storage) storeOf(ctx context.Context, name, region string, connection *uuid.UUID) (bucketClient, error) {
	if connection == nil && region == s.platform.region {
		b := s.platform
		b.name = name
		return b, nil
	}
	if connection != nil && s.connections == nil {
		return bucketClient{}, ErrBucketsUnconfigured
	}
	key := region
	if connection != nil {
		key += " " + connection.String()
	}
	s.mu.Lock()
	b, ok := s.clients[key]
	s.mu.Unlock()
	if !ok {
		options := []func(*s3.Options){func(o *s3.Options) { o.Region = region }}
		if connection != nil {
			account, err := s.connectedAccount(ctx, *connection)
			if err != nil {
				return bucketClient{}, err
			}
			endpoint, credentials := s.connections.S3Endpoint(), s.connectionCredentials(*connection)
			options = append(options, expectOwner(account.AWSAccountID), func(o *s3.Options) {
				o.Credentials, o.BaseEndpoint, o.UsePathStyle = credentials, nil, endpoint != ""
				if endpoint != "" {
					o.BaseEndpoint = aws.String(endpoint)
				}
			})
		}
		client := s3.New(s.platform.client.Options(), options...)
		b = bucketClient{region: region, connection: connection, client: client, presign: s3.NewPresignClient(client)}
		s.mu.Lock()
		if cached, ok := s.clients[key]; ok {
			b = cached
		} else {
			s.clients[key] = b
		}
		s.mu.Unlock()
	}
	b.name = name
	return b, nil
}

// endpointOf is the S3 endpoint of the platform's buckets, or of a
// connected account's: AWS S3 in the bucket's region unless compute names
// another. The platform's own store, such as Garage or R2, never holds a
// connected account's bucket.
func (s *Storage) endpointOf(connection *uuid.UUID) string {
	if connection == nil {
		return s.config.Endpoint
	}
	return s.connections.S3Endpoint()
}

// expectOwner makes every request but CreateBucket name account as the
// owner of its bucket, so S3 refuses a bucket of the same name that
// another account holds. Presigned URLs carry it as a query parameter. It
// replaces the owner of the options it extends.
func expectOwner(account string) func(*s3.Options) {
	const id = "ExpectedBucketOwner"
	owner := middleware.BuildMiddlewareFunc(id, func(ctx context.Context, in middleware.BuildInput, next middleware.BuildHandler) (middleware.BuildOutput, middleware.Metadata, error) {
		if req, ok := in.Request.(*smithyhttp.Request); ok && awsmiddleware.GetOperationName(ctx) != "CreateBucket" {
			req.Header.Set("X-Amz-Expected-Bucket-Owner", account)
		}
		return next.HandleBuild(ctx, in)
	})
	return func(o *s3.Options) {
		o.APIOptions = append(slices.Clone(o.APIOptions), func(stack *middleware.Stack) error {
			if _, ok := stack.Build.Get(id); ok {
				_, err := stack.Build.Swap(id, owner)
				return err //nolint:wrapcheck // The SDK reports the stack's own error.
			}
			return stack.Build.Add(owner, middleware.After)
		})
	}
}

// connectedAccount is the account connection reaches and its active role.
// Without an active authorization the platform cannot act there, which is
// a *ConflictError: it fails what needs the account's storage rather than
// being retried.
func (s *Storage) connectedAccount(ctx context.Context, connection uuid.UUID) (compute.ConnectedAccount, error) {
	account, err := s.connections.ConnectedAccount(ctx, connection)
	var refused *compute.ConflictError
	if errors.As(err, &refused) {
		return compute.ConnectedAccount{}, conflict("%s", refused.Message)
	}
	if err != nil {
		return compute.ConnectedAccount{}, fmt.Errorf("connection %s: %w", connection, err)
	}
	return account, nil
}

// connectionCredentials are the connection role's, for storage's own
// requests on the account's buckets, renewed credentialWindow before they
// expire.
func (s *Storage) connectionCredentials(connection uuid.UUID) aws.CredentialsProvider {
	return aws.NewCredentialsCache(aws.CredentialsProviderFunc(func(ctx context.Context) (aws.Credentials, error) {
		account, err := s.connectedAccount(ctx, connection)
		if err != nil {
			return aws.Credentials{}, err
		}
		creds, err := s.connections.AssumeConnectionRole(ctx, account, "lazycloud-storage", "", time.Hour)
		if err != nil {
			return aws.Credentials{}, fmt.Errorf("connection %s: %w", connection, err)
		}
		return creds, nil
	}), renewEarly)
}

// providerOf is the provider that creates store's bucket and issues host
// credentials for it: the platform's own, or for a connected account's
// bucket, its connection role.
func (s *Storage) providerOf(ctx context.Context, store bucketClient) (bucketProvider, error) {
	if store.connection == nil {
		if s.buckets == nil {
			return nil, ErrBucketsUnconfigured
		}
		return s.buckets, nil
	}
	account, err := s.connectedAccount(ctx, *store.connection)
	if err != nil {
		return nil, err
	}
	return &awsBuckets{account: account.AWSAccountID, assume: func(ctx context.Context, session, policy string, lifetime time.Duration) (aws.Credentials, error) {
		return s.connections.AssumeConnectionRole(ctx, account, session, policy, lifetime)
	}}, nil
}

// workspaceStore returns the workspace's bucket, creating it on first use.
// The object store's refusal is a *StoreRefusedError, and a connection the
// platform cannot act through a *ConflictError.
func (s *Storage) workspaceStore(ctx context.Context, workspace identity.WorkspaceID) (bucketClient, error) {
	row, err := s.queries.WorkspaceBucket(ctx, uuid.UUID(workspace))
	if errors.Is(err, pgx.ErrNoRows) {
		return bucketClient{}, ErrNotFound
	}
	if err != nil {
		return bucketClient{}, fmt.Errorf("read workspace bucket: %w", err)
	}
	if row.Bucket != nil && row.Region != nil {
		return s.storeOf(ctx, *row.Bucket, *row.Region, row.ConnectionID)
	}
	return s.createWorkspaceBucket(ctx, workspace, row.ConnectionID)
}

// createWorkspaceBucket creates the workspace's bucket: in the platform's
// store, or in connection's account, in the region of its authorization.
// The row is written after the provider created the bucket, so a recorded
// bucket always exists. When another server recorded the workspace's
// bucket first, that one is the workspace's.
func (s *Storage) createWorkspaceBucket(ctx context.Context, workspace identity.WorkspaceID, connection *uuid.UUID) (bucketClient, error) {
	account, region := s.config.Workspaces.AccountID, s.config.Region
	if connection != nil {
		if s.connections == nil {
			return bucketClient{}, ErrBucketsUnconfigured
		}
		connected, err := s.connectedAccount(ctx, *connection)
		if err != nil {
			return bucketClient{}, err
		}
		account, region = connected.AWSAccountID, connected.Region
	}
	if s.config.Workspaces.Prefix == "" {
		return bucketClient{}, ErrBucketsUnconfigured
	}
	store, err := s.storeOf(ctx, bucketName(s.config.Workspaces.Prefix, account, workspace), region, connection)
	if err != nil {
		return bucketClient{}, err
	}
	provider, err := s.providerOf(ctx, store)
	if err != nil {
		return bucketClient{}, err
	}
	if err := provider.ensureBucket(ctx, store); err != nil {
		return bucketClient{}, storeError(fmt.Errorf("create workspace bucket: %w", err))
	}
	// Hosts and clients upload volume files in parts; the store discards
	// parts of uploads nobody completed.
	if _, err := store.client.PutBucketLifecycleConfiguration(ctx, &s3.PutBucketLifecycleConfigurationInput{
		Bucket: aws.String(store.name),
		LifecycleConfiguration: &s3types.BucketLifecycleConfiguration{Rules: []s3types.LifecycleRule{{
			ID: aws.String("abort-incomplete-uploads"), Status: s3types.ExpirationStatusEnabled,
			Filter:                         &s3types.LifecycleRuleFilter{Prefix: aws.String("")},
			AbortIncompleteMultipartUpload: &s3types.AbortIncompleteMultipartUpload{DaysAfterInitiation: aws.Int32(1)},
		}}},
	}); err != nil {
		return bucketClient{}, storeError(fmt.Errorf("set workspace bucket lifecycle: %w", err))
	}
	if err := s.allowBrowser(ctx, store); err != nil {
		return bucketClient{}, storeError(err)
	}
	recorded, err := s.queries.InsertWorkspaceBucket(ctx, InsertWorkspaceBucketParams{
		WorkspaceID: uuid.UUID(workspace), Bucket: store.name, Region: region,
	})
	if err != nil {
		return bucketClient{}, fmt.Errorf("record workspace bucket: %w", err)
	}
	return s.storeOf(ctx, recorded.Bucket, recorded.Region, connection)
}

// AllowBrowserAccess lets the dashboard's pages use presigned requests on
// the platform bucket, which holds artifacts. Workspace buckets get the same
// rule when they are created.
func (s *Storage) AllowBrowserAccess(ctx context.Context) error {
	return s.allowBrowser(ctx, s.platform)
}

// allowBrowser sets the bucket's CORS rule for the dashboard origin: GET,
// HEAD and PUT of presigned URLs, with ETag readable so multipart uploads
// can name their parts.
func (s *Storage) allowBrowser(ctx context.Context, bucket bucketClient) error {
	if s.config.BrowserOrigin == "" {
		return nil
	}
	if _, err := bucket.client.PutBucketCors(ctx, &s3.PutBucketCorsInput{
		Bucket: aws.String(bucket.name),
		CORSConfiguration: &s3types.CORSConfiguration{CORSRules: []s3types.CORSRule{{
			AllowedOrigins: []string{strings.TrimRight(s.config.BrowserOrigin, "/")},
			AllowedMethods: []string{http.MethodGet, http.MethodHead, http.MethodPut},
			AllowedHeaders: []string{"*"},
			ExposeHeaders:  []string{"ETag"},
			MaxAgeSeconds:  aws.Int32(3600),
		}}},
	}); err != nil {
		return fmt.Errorf("allow the dashboard on bucket %s: %w", bucket.name, err)
	}
	return nil
}

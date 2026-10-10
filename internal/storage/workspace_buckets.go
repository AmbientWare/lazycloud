package storage

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"net/http"
	"slices"
	"strings"
	"sync"
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

// objectClient is an S3 client signed for one account and region, its
// presigner, and the account whose buckets it reaches.
type objectClient struct {
	client  *s3.Client
	presign *s3.PresignClient
	account string
}

// bucketClient is a bucket and the client that reaches it.
type bucketClient struct {
	name string
	objectClient
}

// workspaceStore is a workspace's bucket: the client that reaches it, the
// region it was created in and the connected account that holds it, nil
// for the platform's.
type workspaceStore struct {
	bucketClient
	region     string
	connection *uuid.UUID
}

// accountRegion keys the clients of workspace buckets by account and
// region.
type accountRegion struct {
	connected  bool
	connection uuid.UUID
	region     string
}

// accountClients holds the S3 clients of workspace buckets outside the
// platform store's own account and region, made on first use. A
// connection's client assumes the connection's active role whenever its
// credentials renew, so a reconnect's new role takes over within the hour.
type accountClients struct {
	mu      sync.Mutex
	clients map[accountRegion]objectClient
}

// platformClient is the client of the platform's store.
func (s *Storage) platformClient() objectClient {
	return objectClient{client: s.client, presign: s.presign, account: s.config.Workspaces.AccountID}
}

// platformBucket is the platform bucket, which holds sources, artifacts and
// snapshots.
func (s *Storage) platformBucket() bucketClient {
	return bucketClient{name: s.bucket, objectClient: s.platformClient()}
}

// layerBucket is the bucket of converted image layers.
func (s *Storage) layerBucket() bucketClient {
	return bucketClient{name: s.layers, objectClient: s.platformClient()}
}

// clientFor returns the client of the platform's account, or connection's,
// in region. A connection's client names the connected account as the
// owner of every bucket it reaches.
func (s *Storage) clientFor(ctx context.Context, connection *uuid.UUID, region string) (objectClient, error) {
	if connection == nil && region == s.config.Region {
		return s.platformClient(), nil
	}
	if connection != nil && s.connections == nil {
		return objectClient{}, ErrBucketsUnconfigured
	}
	key := accountRegion{region: region}
	if connection != nil {
		key.connected, key.connection = true, *connection
	}
	s.accounts.mu.Lock()
	c, ok := s.accounts.clients[key]
	s.accounts.mu.Unlock()
	if ok {
		return c, nil
	}
	account, credentials, options := s.config.Workspaces.AccountID, s.client.Options().Credentials, []func(*s3.Options){}
	if connection != nil {
		connected, err := s.connections.ConnectedAccount(ctx, *connection)
		var refused *compute.ConflictError
		if errors.As(err, &refused) {
			return objectClient{}, conflict("%s", refused.Message)
		}
		if err != nil {
			return objectClient{}, fmt.Errorf("read the workspace's AWS account: %w", err)
		}
		account, credentials = connected.AWSAccountID, s.connectionCredentials(*connection)
		options = append(options, expectOwner(account))
	}
	client := s3.New(s.client.Options(), append(options, func(o *s3.Options) {
		o.Region, o.Credentials = region, credentials
	})...)
	c = objectClient{client: client, presign: s3.NewPresignClient(client), account: account}
	s.accounts.mu.Lock()
	defer s.accounts.mu.Unlock()
	if cached, ok := s.accounts.clients[key]; ok {
		return cached, nil
	}
	s.accounts.clients[key] = c
	return c, nil
}

// expectOwner makes every request but CreateBucket name account as the
// owner of its bucket, so S3 refuses a bucket of the same name that
// another account holds. Presigned URLs carry it as a query parameter.
func expectOwner(account string) func(*s3.Options) {
	owner := middleware.BuildMiddlewareFunc("ExpectedBucketOwner", func(ctx context.Context, in middleware.BuildInput, next middleware.BuildHandler) (middleware.BuildOutput, middleware.Metadata, error) {
		if req, ok := in.Request.(*smithyhttp.Request); ok && awsmiddleware.GetOperationName(ctx) != "CreateBucket" {
			req.Header.Set("X-Amz-Expected-Bucket-Owner", account)
		}
		return next.HandleBuild(ctx, in)
	})
	return func(o *s3.Options) {
		o.APIOptions = append(slices.Clone(o.APIOptions), func(stack *middleware.Stack) error {
			return stack.Build.Add(owner, middleware.After)
		})
	}
}

// connectionCredentials are the connection role's, for storage's own
// requests on the account's buckets, renewed credentialWindow before they
// expire.
func (s *Storage) connectionCredentials(connection uuid.UUID) aws.CredentialsProvider {
	return aws.NewCredentialsCache(aws.CredentialsProviderFunc(func(ctx context.Context) (aws.Credentials, error) {
		creds, err := s.connections.AssumeConnectionRole(ctx, connection, "lazycloud-storage", "", time.Hour)
		if err != nil {
			return aws.Credentials{}, fmt.Errorf("connection %s: %w", connection, err)
		}
		return creds, nil
	}), renewEarly)
}

// storeOf is the workspace bucket named bucket.
func (s *Storage) storeOf(ctx context.Context, bucket, region string, connection *uuid.UUID) (workspaceStore, error) {
	c, err := s.clientFor(ctx, connection, region)
	if err != nil {
		return workspaceStore{}, err
	}
	return workspaceStore{bucketClient: bucketClient{name: bucket, objectClient: c}, region: region, connection: connection}, nil
}

// providerOf is the provider that creates store's bucket and issues host
// credentials for it: the platform's own, or for a connected account's
// bucket, its connection role.
func (s *Storage) providerOf(store workspaceStore) (bucketProvider, error) {
	if store.connection == nil {
		if s.buckets == nil {
			return nil, ErrBucketsUnconfigured
		}
		return s.buckets, nil
	}
	connection := *store.connection
	return &awsBuckets{assume: func(ctx context.Context, session, policy string, lifetime time.Duration) (aws.Credentials, error) {
		return s.connections.AssumeConnectionRole(ctx, connection, session, policy, lifetime)
	}}, nil
}

// workspaceStore returns the workspace's bucket, creating it on first use:
// in the platform's store, or in the connected account the workspace lives
// in, in the region of the connection's authorization. The row is written
// after the provider created the bucket, so a recorded bucket always
// exists. The object store's refusal is a *StoreRefusedError, and a
// connection the platform cannot act through a *ConflictError.
func (s *Storage) workspaceStore(ctx context.Context, workspace identity.WorkspaceID) (workspaceStore, error) {
	row, err := s.queries.WorkspaceBucket(ctx, uuid.UUID(workspace))
	if err == nil {
		return s.storeOf(ctx, row.Bucket, row.Region, row.ConnectionID)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return workspaceStore{}, fmt.Errorf("read workspace bucket: %w", err)
	}
	connection, err := s.queries.WorkspaceConnection(ctx, uuid.UUID(workspace))
	if errors.Is(err, pgx.ErrNoRows) {
		return workspaceStore{}, ErrNotFound
	}
	if err != nil {
		return workspaceStore{}, fmt.Errorf("read workspace connection: %w", err)
	}
	account, region := s.config.Workspaces.AccountID, s.config.Region
	if connection != nil {
		if s.connections == nil {
			return workspaceStore{}, ErrBucketsUnconfigured
		}
		connected, err := s.connections.ConnectedAccount(ctx, *connection)
		var refused *compute.ConflictError
		if errors.As(err, &refused) {
			return workspaceStore{}, conflict("%s", refused.Message)
		}
		if err != nil {
			return workspaceStore{}, fmt.Errorf("read the workspace's AWS account: %w", err)
		}
		account, region = connected.AWSAccountID, connected.Region
	}
	if s.config.Workspaces.Prefix == "" {
		return workspaceStore{}, ErrBucketsUnconfigured
	}
	store, err := s.storeOf(ctx, bucketName(s.config.Workspaces.Prefix, account, workspace), region, connection)
	if err != nil {
		return workspaceStore{}, err
	}
	provider, err := s.providerOf(store)
	if err != nil {
		return workspaceStore{}, err
	}
	if err := provider.ensureBucket(ctx, store); err != nil {
		return workspaceStore{}, storeError(fmt.Errorf("create workspace bucket: %w", err))
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
		return workspaceStore{}, storeError(fmt.Errorf("set workspace bucket lifecycle: %w", err))
	}
	if err := s.allowBrowser(ctx, store.bucketClient); err != nil {
		return workspaceStore{}, storeError(err)
	}
	if err := s.queries.InsertWorkspaceBucket(ctx, InsertWorkspaceBucketParams{
		WorkspaceID: uuid.UUID(workspace), Bucket: store.name, Region: region, ConnectionID: connection,
	}); err != nil {
		return workspaceStore{}, fmt.Errorf("record workspace bucket: %w", err)
	}
	return store, nil
}

// storeAt is the recorded workspace bucket of a row that left-joins
// workspace_buckets; false when the workspace has none.
func (s *Storage) storeAt(ctx context.Context, bucket, region *string, connection *uuid.UUID) (workspaceStore, bool, error) {
	if bucket == nil || region == nil {
		return workspaceStore{}, false, nil
	}
	store, err := s.storeOf(ctx, *bucket, *region, connection)
	return store, err == nil, err
}

// AllowBrowserAccess lets the dashboard's pages use presigned requests on
// the platform bucket, which holds artifacts. Workspace buckets get the same
// rule when they are created.
func (s *Storage) AllowBrowserAccess(ctx context.Context) error {
	return s.allowBrowser(ctx, s.platformBucket())
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

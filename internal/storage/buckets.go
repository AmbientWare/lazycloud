package storage

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/aws-sdk-go-v2/service/sts"
	"github.com/aws/smithy-go"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// BucketProvider names the object store that creates workspace buckets and
// issues credentials scoped to one of them.
type BucketProvider string

const (
	// ProviderGarage uses the Garage admin API: a bucket per workspace and an
	// expiring key allowed on that bucket alone.
	ProviderGarage BucketProvider = "garage"
	// ProviderAWS uses S3 buckets and STS AssumeRole with a session policy
	// limited to the workspace bucket.
	ProviderAWS BucketProvider = "aws"
)

// WorkspaceBuckets configures the buckets that hold volumes and disks.
type WorkspaceBuckets struct {
	Provider BucketProvider
	// Prefix starts every workspace bucket name: <prefix>-<workspace id hex>.
	// Bucket names are global on AWS, so deployments use distinct prefixes.
	Prefix string
	// GarageAdminURL and GarageAdminToken reach the Garage admin API.
	GarageAdminURL   string
	GarageAdminToken string
	// RoleARN is the role STS issues host credentials for. Its permissions
	// must cover every workspace bucket; the session policy narrows them.
	RoleARN string
}

// ErrBucketsUnconfigured means volumes and disks were used on a server
// without a workspace bucket provider.
var ErrBucketsUnconfigured = errors.New("workspace buckets are not configured")

// grantLifetime is how long host credentials last. Hosts get new ones well
// before they expire.
const grantLifetime = time.Hour

// Grant is a credential that reaches one workspace bucket until ExpiresAt.
type Grant struct {
	Endpoint        string
	Region          string
	Bucket          string
	AccessKeyID     string
	SecretAccessKey string
	SessionToken    string
	ExpiresAt       time.Time
}

// bucketProvider creates workspace buckets and scoped credentials. Garage
// and AWS are the two object stores the platform runs on.
type bucketProvider interface {
	// ensureBucket creates the bucket if it is missing and lets the
	// platform's own key use it.
	ensureBucket(ctx context.Context, bucket string) error
	// issue returns a credential for bucket alone. revocable means the key
	// must be deleted after it expires.
	issue(ctx context.Context, bucket, name string, lifetime time.Duration) (grant Grant, revocable bool, err error)
	revoke(ctx context.Context, accessKeyID string) error
}

func newBucketProvider(cfg Config, client *s3.Client) bucketProvider {
	switch cfg.Workspaces.Provider {
	case ProviderGarage:
		return &garageBuckets{
			adminURL: strings.TrimRight(cfg.Workspaces.GarageAdminURL, "/"), token: cfg.Workspaces.GarageAdminToken,
			platformKey: cfg.AccessKeyID, http: &http.Client{Timeout: 30 * time.Second},
		}
	case ProviderAWS:
		return &awsBuckets{
			s3: client, region: cfg.Region, roleARN: cfg.Workspaces.RoleARN,
			sts: sts.New(sts.Options{
				Region:      cfg.Region,
				Credentials: credentials.NewStaticCredentialsProvider(cfg.AccessKeyID, cfg.SecretAccessKey, ""),
			}),
		}
	}
	return nil
}

func (s *Storage) bucketName(workspace identity.WorkspaceID) string {
	return s.config.Workspaces.Prefix + "-" + strings.ReplaceAll(uuid.UUID(workspace).String(), "-", "")
}

// workspaceBucket returns the workspace's bucket, creating it on first use.
// The row is written after the provider created the bucket, so a recorded
// bucket always exists.
func (s *Storage) workspaceBucket(ctx context.Context, workspace identity.WorkspaceID) (string, error) {
	bucket, err := s.queries.WorkspaceBucket(ctx, uuid.UUID(workspace))
	if err == nil {
		return bucket, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return "", fmt.Errorf("read workspace bucket: %w", err)
	}
	if s.buckets == nil {
		return "", ErrBucketsUnconfigured
	}
	bucket = s.bucketName(workspace)
	if err := s.buckets.ensureBucket(ctx, bucket); err != nil {
		return "", fmt.Errorf("create workspace bucket: %w", err)
	}
	// Hosts and clients upload volume files in parts; the store discards
	// parts of uploads nobody completed.
	if _, err := s.client.PutBucketLifecycleConfiguration(ctx, &s3.PutBucketLifecycleConfigurationInput{
		Bucket: aws.String(bucket),
		LifecycleConfiguration: &s3types.BucketLifecycleConfiguration{Rules: []s3types.LifecycleRule{{
			ID: aws.String("abort-incomplete-uploads"), Status: s3types.ExpirationStatusEnabled,
			Filter:                         &s3types.LifecycleRuleFilter{Prefix: aws.String("")},
			AbortIncompleteMultipartUpload: &s3types.AbortIncompleteMultipartUpload{DaysAfterInitiation: aws.Int32(1)},
		}}},
	}); err != nil {
		return "", fmt.Errorf("set workspace bucket lifecycle: %w", err)
	}
	if err := s.allowBrowser(ctx, bucket); err != nil {
		return "", err
	}
	if err := s.queries.InsertWorkspaceBucket(ctx, InsertWorkspaceBucketParams{WorkspaceID: uuid.UUID(workspace), Bucket: bucket}); err != nil {
		return "", fmt.Errorf("record workspace bucket: %w", err)
	}
	return bucket, nil
}

// AllowBrowserAccess lets the dashboard's pages use presigned requests on
// the platform bucket, which holds artifacts. Workspace buckets get the same
// rule when they are created.
func (s *Storage) AllowBrowserAccess(ctx context.Context) error {
	return s.allowBrowser(ctx, s.bucket)
}

// allowBrowser sets the bucket's CORS rule for the dashboard origin: GET,
// HEAD and PUT of presigned URLs, with ETag readable so multipart uploads
// can name their parts.
func (s *Storage) allowBrowser(ctx context.Context, bucket string) error {
	if s.config.BrowserOrigin == "" {
		return nil
	}
	if _, err := s.client.PutBucketCors(ctx, &s3.PutBucketCorsInput{
		Bucket: aws.String(bucket),
		CORSConfiguration: &s3types.CORSConfiguration{CORSRules: []s3types.CORSRule{{
			AllowedOrigins: []string{strings.TrimRight(s.config.BrowserOrigin, "/")},
			AllowedMethods: []string{http.MethodGet, http.MethodHead, http.MethodPut},
			AllowedHeaders: []string{"*"},
			ExposeHeaders:  []string{"ETag"},
			MaxAgeSeconds:  aws.Int32(3600),
		}}},
	}); err != nil {
		return fmt.Errorf("allow the dashboard on bucket %s: %w", bucket, err)
	}
	return nil
}

type garageBuckets struct {
	adminURL    string
	token       string
	platformKey string
	http        *http.Client
}

// garageError is a failed admin API call.
type garageError struct {
	Op     string
	Status int
	Body   string
}

func (e *garageError) Error() string {
	return fmt.Sprintf("garage %s: HTTP %d: %s", e.Op, e.Status, e.Body)
}

func (g *garageBuckets) call(ctx context.Context, method, op string, query url.Values, body, out any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode garage %s: %w", op, err)
		}
		reader = bytes.NewReader(encoded)
	}
	target := g.adminURL + "/v2/" + op
	if len(query) > 0 {
		target += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, target, reader)
	if err != nil {
		return fmt.Errorf("garage %s: %w", op, err)
	}
	req.Header.Set("Authorization", "Bearer "+g.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := g.http.Do(req)
	if err != nil {
		return fmt.Errorf("garage %s: %w", op, err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return fmt.Errorf("garage %s: read response: %w", op, err)
	}
	if resp.StatusCode >= 300 {
		return &garageError{Op: op, Status: resp.StatusCode, Body: strings.TrimSpace(string(data))}
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			return fmt.Errorf("garage %s: decode response: %w", op, err)
		}
	}
	return nil
}

type garagePerms struct {
	Read  bool `json:"read"`
	Write bool `json:"write"`
	Owner bool `json:"owner"`
}

func (g *garageBuckets) allow(ctx context.Context, bucketID, key string, perms garagePerms) error {
	return g.call(ctx, http.MethodPost, "AllowBucketKey", nil, map[string]any{
		"bucketId": bucketID, "accessKeyId": key, "permissions": perms,
	}, nil)
}

func (g *garageBuckets) bucketID(ctx context.Context, bucket string) (string, error) {
	var info struct {
		ID string `json:"id"`
	}
	err := g.call(ctx, http.MethodGet, "GetBucketInfo", url.Values{"globalAlias": {bucket}}, nil, &info)
	return info.ID, err
}

func (g *garageBuckets) ensureBucket(ctx context.Context, bucket string) error {
	id, err := g.bucketID(ctx, bucket)
	var missing *garageError
	if errors.As(err, &missing) && missing.Status == http.StatusNotFound {
		var created struct {
			ID string `json:"id"`
		}
		err = g.call(ctx, http.MethodPost, "CreateBucket", nil, map[string]string{"globalAlias": bucket}, &created)
		if errors.As(err, &missing) && missing.Status == http.StatusConflict {
			// Another server created it first.
			id, err = g.bucketID(ctx, bucket)
		} else {
			id = created.ID
		}
	}
	if err != nil {
		return err
	}
	return g.allow(ctx, id, g.platformKey, garagePerms{Read: true, Write: true, Owner: true})
}

func (g *garageBuckets) issue(ctx context.Context, bucket, name string, lifetime time.Duration) (Grant, bool, error) {
	id, err := g.bucketID(ctx, bucket)
	if err != nil {
		return Grant{}, false, err
	}
	expires := time.Now().Add(lifetime).UTC().Truncate(time.Second)
	var key struct {
		AccessKeyID     string `json:"accessKeyId"`
		SecretAccessKey string `json:"secretAccessKey"`
	}
	if err := g.call(ctx, http.MethodPost, "CreateKey", nil, map[string]any{
		"name": name, "expiration": expires.Format(time.RFC3339),
	}, &key); err != nil {
		return Grant{}, false, err
	}
	if err := g.allow(ctx, id, key.AccessKeyID, garagePerms{Read: true, Write: true}); err != nil {
		// The key reaches nothing yet; delete it now rather than at expiry.
		return Grant{}, false, errors.Join(err, g.revoke(context.WithoutCancel(ctx), key.AccessKeyID))
	}
	return Grant{Bucket: bucket, AccessKeyID: key.AccessKeyID, SecretAccessKey: key.SecretAccessKey, ExpiresAt: expires}, true, nil
}

func (g *garageBuckets) revoke(ctx context.Context, accessKeyID string) error {
	err := g.call(ctx, http.MethodPost, "DeleteKey", url.Values{"id": {accessKeyID}}, nil, nil)
	var missing *garageError
	if errors.As(err, &missing) && missing.Status == http.StatusNotFound {
		return nil
	}
	return err
}

type awsBuckets struct {
	s3      *s3.Client
	sts     *sts.Client
	region  string
	roleARN string
}

func (a *awsBuckets) ensureBucket(ctx context.Context, bucket string) error {
	input := &s3.CreateBucketInput{Bucket: aws.String(bucket)}
	if a.region != "" && a.region != "us-east-1" {
		input.CreateBucketConfiguration = &s3types.CreateBucketConfiguration{
			LocationConstraint: s3types.BucketLocationConstraint(a.region),
		}
	}
	_, err := a.s3.CreateBucket(ctx, input)
	var apiErr smithy.APIError
	if errors.As(err, &apiErr) && apiErr.ErrorCode() == "BucketAlreadyOwnedByYou" {
		return nil
	}
	if err != nil {
		return fmt.Errorf("create bucket %s: %w", bucket, err)
	}
	return nil
}

func (a *awsBuckets) issue(ctx context.Context, bucket, name string, lifetime time.Duration) (Grant, bool, error) {
	policy, err := json.Marshal(hostPolicy(bucket))
	if err != nil {
		return Grant{}, false, fmt.Errorf("encode session policy: %w", err)
	}
	out, err := a.sts.AssumeRole(ctx, &sts.AssumeRoleInput{
		RoleArn:         aws.String(a.roleARN),
		RoleSessionName: aws.String(name),
		DurationSeconds: aws.Int32(int32(lifetime.Seconds())),
		Policy:          aws.String(string(policy)),
	})
	if err != nil {
		return Grant{}, false, fmt.Errorf("assume role for %s: %w", bucket, err)
	}
	c := out.Credentials
	return Grant{
		Bucket: bucket, AccessKeyID: aws.ToString(c.AccessKeyId), SecretAccessKey: aws.ToString(c.SecretAccessKey),
		SessionToken: aws.ToString(c.SessionToken), ExpiresAt: aws.ToTime(c.Expiration),
	}, false, nil
}

func (a *awsBuckets) revoke(context.Context, string) error { return nil }

// hostPolicy is the STS session policy of a host grant: object reads,
// writes and multipart uploads under the bucket's volumes/ and disks/
// prefixes, and listing those prefixes. It grants nothing on the bucket
// itself, such as its policy, lifecycle or deletion.
func hostPolicy(bucket string) map[string]any {
	arn := "arn:aws:s3:::" + bucket
	return map[string]any{
		"Version": "2012-10-17",
		"Statement": []map[string]any{
			{
				"Effect": "Allow",
				"Action": []string{
					"s3:GetObject", "s3:PutObject", "s3:DeleteObject",
					"s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
				},
				"Resource": []string{arn + "/volumes/*", arn + "/disks/*"},
			},
			{
				"Effect":    "Allow",
				"Action":    []string{"s3:ListBucket", "s3:ListBucketMultipartUploads"},
				"Resource":  []string{arn},
				"Condition": map[string]any{"StringLike": map[string]any{"s3:prefix": []string{"volumes/*", "disks/*"}}},
			},
		},
	}
}

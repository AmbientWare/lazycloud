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
	"strconv"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/aws/retry"
	awshttp "github.com/aws/aws-sdk-go-v2/aws/transport/http"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/aws-sdk-go-v2/service/sts"
	"github.com/aws/smithy-go"
)

// BucketProvider names the object store that creates the platform's
// workspace buckets and issues credentials scoped to one of them.
type BucketProvider string

const (
	// ProviderGarage uses the Garage admin API: a bucket per workspace and an
	// expiring key allowed on that bucket alone.
	ProviderGarage BucketProvider = "garage"
	// ProviderAWS uses S3 buckets and STS AssumeRole with a session policy
	// limited to the workspace bucket.
	ProviderAWS BucketProvider = "aws"
)

// WorkspaceBuckets configures the buckets that hold volumes and disks. A
// workspace on the platform's compute keeps its bucket in the platform's
// store; one in a connected AWS account keeps it in that account, created
// and reached through the connection's role.
type WorkspaceBuckets struct {
	Provider BucketProvider
	// Prefix starts every workspace bucket name (bucketName), at most
	// maxPrefix characters. A connection role manages only the buckets
	// named <prefix>-<its account id>-*.
	Prefix string
	// AccountID is the platform's 12-digit account id, which names its
	// workspace buckets.
	AccountID string
	// GarageAdminURL and GarageAdminToken reach the Garage admin API.
	GarageAdminURL   string
	GarageAdminToken string
	// RoleARN is the role STS issues host credentials for. Its permissions
	// must cover every platform workspace bucket; the session policy
	// narrows them.
	RoleARN string
}

// ErrBucketsUnconfigured means volumes and disks were used on a server
// without a workspace bucket provider for the workspace's account.
var ErrBucketsUnconfigured = errors.New("workspace buckets are not configured")

// grantLifetime is how long host credentials last. Hosts get new ones well
// before they expire.
const grantLifetime = time.Hour

// Grant is a credential that reaches one workspace bucket until ExpiresAt.
type Grant struct {
	Location
	AccessKeyID     string
	SecretAccessKey string
	SessionToken    string
	ExpiresAt       time.Time
}

// bucketProvider creates workspace buckets and scoped credentials. Garage
// and AWS are the two object stores the platform runs on.
type bucketProvider interface {
	// ensureBucket creates the store's bucket if it is missing and lets
	// the store's client use it.
	ensureBucket(ctx context.Context, store workspaceStore) error
	// issue returns a credential for bucket alone. revocable means the key
	// must be deleted after it expires.
	issue(ctx context.Context, bucket, name string, lifetime time.Duration) (grant Grant, revocable bool, err error)
	// issueRead returns a credential that reads the objects under prefix
	// in bucket, or where the store cannot scope it, the bucket.
	issueRead(ctx context.Context, bucket, prefix, name string, lifetime time.Duration) (grant Grant, revocable bool, err error)
	revoke(ctx context.Context, accessKeyID string) error
}

// newBucketProvider is the provider of the platform's workspace buckets.
func newBucketProvider(cfg Config) bucketProvider {
	switch cfg.Workspaces.Provider {
	case ProviderGarage:
		return &garageBuckets{
			adminURL: strings.TrimRight(cfg.Workspaces.GarageAdminURL, "/"), token: cfg.Workspaces.GarageAdminToken,
			platformKey: cfg.AccessKeyID, http: &http.Client{Timeout: 30 * time.Second},
		}
	case ProviderAWS:
		client := sts.New(sts.Options{Region: cfg.Region, Credentials: credentialProvider(cfg)})
		role := cfg.Workspaces.RoleARN
		return &awsBuckets{assume: func(ctx context.Context, session, policy string, lifetime time.Duration) (aws.Credentials, error) {
			out, err := client.AssumeRole(ctx, &sts.AssumeRoleInput{
				RoleArn: aws.String(role), RoleSessionName: aws.String(session),
				DurationSeconds: aws.Int32(int32(lifetime.Seconds())), Policy: aws.String(policy),
			})
			if err != nil {
				return aws.Credentials{}, fmt.Errorf("assume %s: %w", role, err)
			}
			c := out.Credentials
			return aws.Credentials{
				AccessKeyID: aws.ToString(c.AccessKeyId), SecretAccessKey: aws.ToString(c.SecretAccessKey),
				SessionToken: aws.ToString(c.SessionToken), CanExpire: true, Expires: aws.ToTime(c.Expiration),
			}, nil
		}}
	}
	return nil
}

// StoreRefusedError is the object store refusing a request with a client
// error that retrying does not fix, such as access denied. Code is the
// store's error code, or the HTTP status where it gives none.
type StoreRefusedError struct {
	Code string
	Err  error
}

func (e *StoreRefusedError) Error() string { return e.Err.Error() }

func (e *StoreRefusedError) Unwrap() error { return e.Err }

// storeError returns err as a *StoreRefusedError when the object store
// answered it with a 4xx that retrying does not fix: not a timeout or
// throttling. Any other error is returned as it is, for the caller to
// retry.
func storeError(err error) error {
	var garage *garageError
	if errors.As(err, &garage) {
		if !refusedStatus(garage.Status) {
			return err
		}
		return &StoreRefusedError{Code: "HTTP " + strconv.Itoa(garage.Status), Err: err}
	}
	var response *awshttp.ResponseError
	var api smithy.APIError
	if !errors.As(err, &response) || !errors.As(err, &api) || !refusedStatus(response.HTTPStatusCode()) ||
		retry.IsErrorThrottles(retry.DefaultThrottles).IsErrorThrottle(err) == aws.TrueTernary ||
		retry.IsErrorRetryables(retry.DefaultRetryables).IsErrorRetryable(err) == aws.TrueTernary {
		return err
	}
	return &StoreRefusedError{Code: api.ErrorCode(), Err: err}
}

func refusedStatus(status int) bool {
	return status >= 400 && status < 500 && status != http.StatusRequestTimeout && status != http.StatusTooManyRequests
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

func (g *garageBuckets) ensureBucket(ctx context.Context, store workspaceStore) error {
	bucket := store.name
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
	return g.issueWith(ctx, bucket, name, lifetime, garagePerms{Read: true, Write: true})
}

// issueWith creates a key expiring after lifetime with perms on bucket.
func (g *garageBuckets) issueWith(ctx context.Context, bucket, name string, lifetime time.Duration, perms garagePerms) (Grant, bool, error) {
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
	if err := g.allow(ctx, id, key.AccessKeyID, perms); err != nil {
		// The key reaches nothing yet; delete it now rather than at expiry.
		return Grant{}, false, errors.Join(err, g.revoke(context.WithoutCancel(ctx), key.AccessKeyID))
	}
	return Grant{Location: Location{Bucket: bucket}, AccessKeyID: key.AccessKeyID, SecretAccessKey: key.SecretAccessKey, ExpiresAt: expires}, true, nil
}

func (g *garageBuckets) revoke(ctx context.Context, accessKeyID string) error {
	err := g.call(ctx, http.MethodPost, "DeleteKey", url.Values{"id": {accessKeyID}}, nil, nil)
	var missing *garageError
	if errors.As(err, &missing) && missing.Status == http.StatusNotFound {
		return nil
	}
	return err
}

// awsBuckets creates S3 buckets and issues host credentials by assuming a
// role with a session policy limited to one bucket: the platform's
// workspace storage role, or a connected account's connection role.
type awsBuckets struct {
	assume func(ctx context.Context, session, policy string, lifetime time.Duration) (aws.Credentials, error)
}

func (a *awsBuckets) ensureBucket(ctx context.Context, store workspaceStore) error {
	input := &s3.CreateBucketInput{Bucket: aws.String(store.name)}
	if store.region != "us-east-1" {
		input.CreateBucketConfiguration = &s3types.CreateBucketConfiguration{
			LocationConstraint: s3types.BucketLocationConstraint(store.region),
		}
	}
	_, err := store.client.CreateBucket(ctx, input)
	var apiErr smithy.APIError
	if errors.As(err, &apiErr) && apiErr.ErrorCode() == "BucketAlreadyOwnedByYou" {
		return nil
	}
	if err != nil {
		return fmt.Errorf("create bucket %s: %w", store.name, err)
	}
	return nil
}

func (a *awsBuckets) issue(ctx context.Context, bucket, name string, lifetime time.Duration) (Grant, bool, error) {
	policy, err := json.Marshal(hostPolicy(bucket))
	if err != nil {
		return Grant{}, false, fmt.Errorf("encode session policy: %w", err)
	}
	return a.issueWith(ctx, bucket, name, string(policy), lifetime)
}

// issueWith assumes the role for bucket under the session policy.
func (a *awsBuckets) issueWith(ctx context.Context, bucket, name, policy string, lifetime time.Duration) (Grant, bool, error) {
	creds, err := a.assume(ctx, name, policy, lifetime)
	if err != nil {
		return Grant{}, false, fmt.Errorf("assume role for %s: %w", bucket, err)
	}
	return Grant{
		Location: Location{Bucket: bucket}, AccessKeyID: creds.AccessKeyID, SecretAccessKey: creds.SecretAccessKey,
		SessionToken: creds.SessionToken, ExpiresAt: creds.Expires,
	}, false, nil
}

func (a *awsBuckets) revoke(context.Context, string) error { return nil }

// hostPolicy is the STS session policy of a host grant: object reads,
// writes and multipart uploads under the bucket's volumes/ and disks/
// prefixes, listing those prefixes, and listing the bucket's multipart
// uploads, which S3 does not condition on a prefix. It grants nothing else
// on the bucket itself, such as its policy, lifecycle or deletion.
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
				"Action":    []string{"s3:ListBucket"},
				"Resource":  []string{arn},
				"Condition": map[string]any{"StringLike": map[string]any{"s3:prefix": []string{"volumes/*", "disks/*"}}},
			},
			{
				"Effect":   "Allow",
				"Action":   []string{"s3:ListBucketMultipartUploads"},
				"Resource": []string{arn},
			},
		},
	}
}

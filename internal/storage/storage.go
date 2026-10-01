// Package storage owns object bytes and their metadata: source archives,
// volumes, disks, artifacts, queues and maps. PostgreSQL holds metadata and
// authority; an S3-compatible object store holds the bytes. Each workspace's
// volumes and disks live in a bucket of their own, so a host can be given
// short-lived credentials that reach one workspace alone.
package storage

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/smithy-go"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	uploadURLLifetime   = 15 * time.Minute
	downloadURLLifetime = time.Hour
)

// ErrInvalidDigest means a digest is not 64 lowercase hex characters.
var ErrInvalidDigest = errors.New("invalid sha256 digest")

// Digest is the SHA-256 of an object's bytes.
type Digest [32]byte

// ParseDigest reads a lowercase hex digest.
func ParseDigest(s string) (Digest, error) {
	var d Digest
	if len(s) != 64 {
		return d, ErrInvalidDigest
	}
	if _, err := hex.Decode(d[:], []byte(s)); err != nil {
		return d, ErrInvalidDigest
	}
	return d, nil
}

func (d Digest) String() string { return hex.EncodeToString(d[:]) }

// Config locates the object store. With Endpoint, as for Garage or R2,
// requests use path-style addressing; without it they go to AWS S3. The key
// pair is optional; without it the AWS default credential chain applies.
type Config struct {
	Endpoint        string
	Region          string
	Bucket          string
	AccessKeyID     string
	SecretAccessKey string
	// Workspaces configures the buckets that hold volumes and disks.
	Workspaces WorkspaceBuckets
	// BrowserOrigin is the dashboard's origin. When set, buckets let pages
	// from it send and read presigned requests: uploads, downloads and
	// previews go from the browser to the store, not through the API.
	BrowserOrigin string
}

// Storage is the storage owner.
type Storage struct {
	pool    *pgxpool.Pool
	queries *Queries
	client  *s3.Client
	presign *s3.PresignClient
	bucket  string
	config  Config
	buckets bucketProvider
	// orphanAge is the sweep's orphanAge; tests in this package shorten it.
	orphanAge time.Duration
}

// NewStorage returns the storage owner over pool and the configured bucket.
func NewStorage(pool *pgxpool.Pool, cfg Config) *Storage {
	var endpoint *string
	if cfg.Endpoint != "" {
		endpoint = aws.String(cfg.Endpoint)
	}
	client := s3.New(s3.Options{
		Region:       cfg.Region,
		BaseEndpoint: endpoint,
		UsePathStyle: cfg.Endpoint != "",
		Credentials:  credentialProvider(cfg),
		// Only send checksums the request asks for; S3-compatible stores
		// differ in their support for the SDK's default trailing checksums.
		RequestChecksumCalculation: aws.RequestChecksumCalculationWhenRequired,
		ResponseChecksumValidation: aws.ResponseChecksumValidationWhenRequired,
	})
	return &Storage{
		pool: pool, queries: New(pool), client: client, presign: s3.NewPresignClient(client), bucket: cfg.Bucket,
		config: cfg, buckets: newBucketProvider(cfg, client), orphanAge: orphanAge,
	}
}

// UploadTarget is a presigned request that stores an object's bytes.
type UploadTarget struct {
	URL       string
	Method    string
	Headers   map[string]string
	ExpiresAt time.Time
}

// SourceUpload is the state of a source archive after registration.
type SourceUpload struct {
	// Present means the archive is stored and recorded for the workspace.
	Present bool
	// Upload is set when the archive must be uploaded and registered again.
	Upload *UploadTarget
}

func sourceKey(workspace identity.WorkspaceID, digest Digest) string {
	return fmt.Sprintf("workspaces/%s/sources/%s.zip", workspace, digest)
}

// RegisterSource records the archive with digest and size for workspace
// when the object store holds it, and otherwise returns a presigned upload.
// The upload request carries the digest, so the store rejects other bytes.
func (s *Storage) RegisterSource(ctx context.Context, workspace identity.WorkspaceID, digest Digest, size int64) (SourceUpload, error) {
	_, err := s.queries.SourceObjectSize(ctx, SourceObjectSizeParams{WorkspaceID: uuid.UUID(workspace), Sha256: digest[:]})
	if err == nil {
		return SourceUpload{Present: true}, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return SourceUpload{}, fmt.Errorf("read source object: %w", err)
	}

	key := sourceKey(workspace, digest)
	stored, err := s.storedMatches(ctx, key, digest, size)
	if err != nil {
		return SourceUpload{}, err
	}
	if stored {
		if err := s.queries.InsertSourceObject(ctx, InsertSourceObjectParams{
			WorkspaceID: uuid.UUID(workspace), Sha256: digest[:], SizeBytes: size,
		}); err != nil {
			return SourceUpload{}, fmt.Errorf("record source object: %w", err)
		}
		return SourceUpload{Present: true}, nil
	}

	req, err := s.presign.PresignPutObject(ctx, &s3.PutObjectInput{
		Bucket:         aws.String(s.bucket),
		Key:            aws.String(key),
		ContentLength:  aws.Int64(size),
		ContentType:    aws.String("application/zip"),
		ChecksumSHA256: aws.String(base64.StdEncoding.EncodeToString(digest[:])),
	}, s3.WithPresignExpires(uploadURLLifetime))
	if err != nil {
		return SourceUpload{}, fmt.Errorf("presign source upload: %w", err)
	}
	headers := map[string]string{}
	for name, values := range req.SignedHeader {
		if len(values) > 0 && http.CanonicalHeaderKey(name) != "Host" {
			headers[name] = values[0]
		}
	}
	return SourceUpload{Upload: &UploadTarget{
		URL: req.URL, Method: req.Method, Headers: headers, ExpiresAt: time.Now().Add(uploadURLLifetime),
	}}, nil
}

// storedMatches reports whether the object at key exists with size bytes and,
// when the store kept a SHA-256 checksum, the expected digest.
func (s *Storage) storedMatches(ctx context.Context, key string, digest Digest, size int64) (bool, error) {
	head, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{
		Bucket:       aws.String(s.bucket),
		Key:          aws.String(key),
		ChecksumMode: types.ChecksumModeEnabled,
	})
	if err != nil {
		var apiErr smithy.APIError
		if errors.As(err, &apiErr) && (apiErr.ErrorCode() == "NotFound" || apiErr.ErrorCode() == "NoSuchKey") {
			return false, nil
		}
		return false, fmt.Errorf("head source object: %w", err)
	}
	if head.ContentLength == nil || *head.ContentLength != size {
		return false, nil
	}
	if head.ChecksumSHA256 != nil && *head.ChecksumSHA256 != base64.StdEncoding.EncodeToString(digest[:]) {
		return false, nil
	}
	return true, nil
}

// SourceURL is a presigned GET for a workspace's source archive.
func (s *Storage) SourceURL(ctx context.Context, workspace identity.WorkspaceID, digest Digest) (string, time.Time, error) {
	req, err := s.presign.PresignGetObject(ctx, &s3.GetObjectInput{
		Bucket: aws.String(s.bucket),
		Key:    aws.String(sourceKey(workspace, digest)),
	}, s3.WithPresignExpires(downloadURLLifetime))
	if err != nil {
		return "", time.Time{}, fmt.Errorf("presign source download: %w", err)
	}
	return req.URL, time.Now().Add(downloadURLLifetime), nil
}

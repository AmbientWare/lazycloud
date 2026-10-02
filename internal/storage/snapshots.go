package storage

import (
	"context"
	"fmt"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// A memory snapshot's archive is workspaces/<workspace>/snapshots/<id>.tar
// in the platform bucket; execution owns the row that says it exists.

// snapshotURLLifetime covers a host checkpointing and uploading a large
// container, or downloading one to restore.
const snapshotURLLifetime = time.Hour

func snapshotKey(workspace identity.WorkspaceID, id uuid.UUID) string {
	return "workspaces/" + workspace.String() + "/snapshots/" + id.String() + ".tar"
}

// SnapshotUploadURL is where a host PUTs a snapshot's archive.
func (s *Storage) SnapshotUploadURL(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID) (string, error) {
	lifetime, err := s.signedLifetime(ctx, snapshotURLLifetime)
	if err != nil {
		return "", err
	}
	req, err := s.presign.PresignPutObject(ctx, &s3.PutObjectInput{
		Bucket: aws.String(s.bucket), Key: aws.String(snapshotKey(workspace, id)),
	}, s3.WithPresignExpires(lifetime))
	if err != nil {
		return "", fmt.Errorf("presign snapshot upload: %w", err)
	}
	return req.URL, nil
}

// SnapshotDownloadURL is where a host GETs a snapshot's archive to restore
// it.
func (s *Storage) SnapshotDownloadURL(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID) (string, error) {
	lifetime, err := s.signedLifetime(ctx, snapshotURLLifetime)
	if err != nil {
		return "", err
	}
	req, err := s.presign.PresignGetObject(ctx, &s3.GetObjectInput{
		Bucket: aws.String(s.bucket), Key: aws.String(snapshotKey(workspace, id)),
	}, s3.WithPresignExpires(lifetime))
	if err != nil {
		return "", fmt.Errorf("presign snapshot download: %w", err)
	}
	return req.URL, nil
}

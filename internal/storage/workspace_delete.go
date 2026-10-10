package storage

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// DeleteWorkspaceStorage deletes up to prefixChunk of the workspace's
// objects, with their multipart uploads: first those under its prefix of
// the platform bucket, then those in its workspace bucket, in the platform
// store or its connected account. Once that bucket is empty it deletes the
// bucket and its row. It reports whether everything is gone; the caller
// repeats until it is, then removes the workspace's rows. Bounding each
// call keeps a pass short however many objects a workspace holds. A
// connected account the platform can no longer act in keeps the bucket,
// which is the customer's to delete; only its row goes.
func (s *Storage) DeleteWorkspaceStorage(ctx context.Context, logger *slog.Logger, workspace identity.WorkspaceID) (bool, error) {
	empty, err := s.deletePrefixChunk(ctx, s.platformBucket(), fmt.Sprintf("workspaces/%s/", workspace))
	if err != nil {
		return false, fmt.Errorf("delete workspace objects: %w", err)
	}
	if !empty {
		return false, nil
	}
	row, err := s.queries.WorkspaceBucket(ctx, uuid.UUID(workspace))
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return false, fmt.Errorf("read workspace bucket: %w", err)
	}
	if row.Bucket == nil || row.Region == nil {
		return true, nil
	}
	empty, err = s.deleteWorkspaceBucket(ctx, *row.Bucket, *row.Region, row.ConnectionID)
	var unauthorized *ConflictError
	if row.ConnectionID != nil && errors.As(err, &unauthorized) {
		logger.WarnContext(ctx, "the workspace's bucket stays in the connected AWS account, which no longer authorizes the platform",
			"workspace", workspace.String(), "bucket", *row.Bucket, "connection", row.ConnectionID.String(), "error", err)
	} else if err != nil || !empty {
		return false, err
	}
	if err := s.queries.DeleteWorkspaceBucket(ctx, uuid.UUID(workspace)); err != nil {
		return false, fmt.Errorf("forget workspace bucket: %w", err)
	}
	return true, nil
}

// deleteWorkspaceBucket empties a chunk of the workspace bucket and, once
// it is empty, deletes it. It reports whether the bucket is gone.
func (s *Storage) deleteWorkspaceBucket(ctx context.Context, bucket, region string, connection *uuid.UUID) (bool, error) {
	store, err := s.storeOf(ctx, bucket, region, connection)
	if err != nil {
		return false, err
	}
	if empty, err := s.deletePrefixChunk(ctx, store.bucketClient, ""); err != nil || !empty {
		if err != nil {
			return false, fmt.Errorf("empty workspace bucket %s: %w", store.name, err)
		}
		return false, nil
	}
	_, err = store.client.DeleteBucket(ctx, &s3.DeleteBucketInput{Bucket: aws.String(store.name)})
	var api smithy.APIError
	switch {
	case errors.As(err, &api) && api.ErrorCode() == "BucketNotEmpty":
		// A host or an upload wrote after the listing; the next call empties
		// the bucket again.
		return false, nil
	case errors.As(err, &api) && api.ErrorCode() == "NoSuchBucket":
	case err != nil:
		return false, fmt.Errorf("delete workspace bucket %s: %w", store.name, err)
	}
	return true, nil
}

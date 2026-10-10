package storage

import (
	"context"
	"errors"
	"fmt"

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
// call keeps a pass short however many objects a workspace holds.
func (s *Storage) DeleteWorkspaceStorage(ctx context.Context, workspace identity.WorkspaceID) (bool, error) {
	empty, err := s.deletePrefixChunk(ctx, s.platformBucket(), fmt.Sprintf("workspaces/%s/", workspace))
	if err != nil {
		return false, fmt.Errorf("delete workspace objects: %w", err)
	}
	if !empty {
		return false, nil
	}
	row, err := s.queries.WorkspaceBucket(ctx, uuid.UUID(workspace))
	if errors.Is(err, pgx.ErrNoRows) {
		return true, nil
	}
	if err != nil {
		return false, fmt.Errorf("read workspace bucket: %w", err)
	}
	store, err := s.storeOf(row.Bucket, row.Region, row.ConnectionID)
	if err != nil {
		return false, err
	}
	if empty, err = s.deletePrefixChunk(ctx, store.bucketClient, ""); err != nil || !empty {
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
	if err := s.queries.DeleteWorkspaceBucket(ctx, uuid.UUID(workspace)); err != nil {
		return false, fmt.Errorf("forget workspace bucket: %w", err)
	}
	return true, nil
}

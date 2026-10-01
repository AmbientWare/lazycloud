package storage

import (
	"context"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// DeleteWorkspaceObjects deletes every object under the workspace's prefix
// and returns how many it deleted. The caller removes the workspace's rows
// afterwards; deleting again after a partial failure finishes the rest.
func (s *Storage) DeleteWorkspaceObjects(ctx context.Context, workspace identity.WorkspaceID) (int, error) {
	prefix := fmt.Sprintf("workspaces/%s/", workspace)
	deleted := 0
	pages := s3.NewListObjectsV2Paginator(s.client, &s3.ListObjectsV2Input{Bucket: aws.String(s.bucket), Prefix: aws.String(prefix)})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return deleted, fmt.Errorf("list workspace objects: %w", err)
		}
		for _, object := range page.Contents {
			if _, err := s.client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(s.bucket), Key: object.Key}); err != nil {
				return deleted, fmt.Errorf("delete %s: %w", aws.ToString(object.Key), err)
			}
			deleted++
		}
	}
	return deleted, nil
}

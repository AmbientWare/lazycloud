package storage

import (
	"context"
	"fmt"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// HostGrant issues host a credential for the workspace bucket, creating the
// bucket on first use. A key that outlives its expiry reaches nothing;
// recording it lets the sweep delete it from the provider.
func (s *Storage) HostGrant(ctx context.Context, host compute.HostID, workspace identity.WorkspaceID) (Grant, error) {
	if s.buckets == nil {
		return Grant{}, ErrBucketsUnconfigured
	}
	bucket, err := s.workspaceBucket(ctx, workspace)
	if err != nil {
		return Grant{}, err
	}
	grant, revocable, err := s.buckets.issue(ctx, bucket, "lazycloud-host-"+host.String(), grantLifetime)
	if err != nil {
		return Grant{}, fmt.Errorf("issue storage grant: %w", err)
	}
	if revocable {
		if err := s.queries.InsertStorageGrant(ctx, InsertStorageGrantParams{
			AccessKeyID: grant.AccessKeyID, WorkspaceID: uuid.UUID(workspace), HostID: uuid.UUID(host), ExpiresAt: grant.ExpiresAt,
		}); err != nil {
			return Grant{}, fmt.Errorf("record storage grant: %w", err)
		}
	}
	grant.Endpoint, grant.Region = s.config.Endpoint, s.config.Region
	return grant, nil
}

// HostMountWorkspaces lists the workspaces whose volumes or disks the host's
// live containers use; the host needs a grant for each.
func (s *Storage) HostMountWorkspaces(ctx context.Context, host compute.HostID) ([]identity.WorkspaceID, error) {
	rows, err := s.queries.HostMountWorkspaces(ctx, hostRef(host))
	if err != nil {
		return nil, fmt.Errorf("list host mount workspaces: %w", err)
	}
	out := make([]identity.WorkspaceID, len(rows))
	for n, id := range rows {
		out[n] = identity.WorkspaceID(id)
	}
	return out, nil
}

// hostRef is a host id for a nullable host column.
func hostRef(host compute.HostID) *uuid.UUID {
	id := uuid.UUID(host)
	return &id
}

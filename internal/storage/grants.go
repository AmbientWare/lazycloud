package storage

import (
	"context"
	"fmt"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// HostGrant issues host a credential for the workspace bucket, creating the
// bucket on first use. The object store's refusal is a *StoreRefusedError.
func (s *Storage) HostGrant(ctx context.Context, host compute.HostID, workspace identity.WorkspaceID) (Grant, error) {
	store, err := s.workspaceStore(ctx, workspace)
	if err != nil {
		return Grant{}, err
	}
	return s.grant(ctx, host, store, uuid.UUID(workspace), "")
}

// grant issues host a credential for store, the bucket of workspace, that
// reads and writes its volumes and disks, or with readPrefix reads the
// objects under it. A connected account's bucket is granted through its
// connection role. A key that outlives its expiry reaches nothing;
// recording it lets the sweep delete it from the provider.
func (s *Storage) grant(ctx context.Context, host compute.HostID, store bucketClient, workspace uuid.UUID, readPrefix string) (Grant, error) {
	provider, err := s.providerOf(ctx, store)
	if err != nil {
		return Grant{}, err
	}
	creds, revocable, err := provider.issue(ctx, store.name, readPrefix, "lazycloud-host-"+host.String(), grantLifetime)
	if err != nil {
		return Grant{}, storeError(fmt.Errorf("issue storage grant: %w", err))
	}
	if revocable {
		if err := s.queries.InsertStorageGrant(ctx, InsertStorageGrantParams{
			AccessKeyID: creds.AccessKeyID, WorkspaceID: workspace, HostID: uuid.UUID(host), ExpiresAt: creds.Expires,
		}); err != nil {
			return Grant{}, fmt.Errorf("record storage grant: %w", err)
		}
	}
	// Hosts address the bucket as the server's own client does.
	endpoint := s.endpointOf(store.connection)
	location, err := locate(endpoint, store.region, store.name, endpoint != "")
	if err != nil {
		return Grant{}, fmt.Errorf("locate workspace bucket: %w", err)
	}
	return Grant{
		Location: location, AccessKeyID: creds.AccessKeyID, SecretAccessKey: creds.SecretAccessKey,
		SessionToken: creds.SessionToken, ExpiresAt: creds.Expires,
	}, nil
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

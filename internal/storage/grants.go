package storage

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// WorkspaceGrant is a host's credential for a workspace bucket and the
// workspace's peer key, which hosts holding its grant sign reads of each
// other's cached volume frames with.
type WorkspaceGrant struct {
	Grant
	PeerKey []byte
}

// HostGrant issues host a credential for the workspace bucket, creating the
// bucket and the workspace's peer key on first use. The object store's
// refusal is a *StoreRefusedError.
func (s *Storage) HostGrant(ctx context.Context, host compute.HostID, workspace identity.WorkspaceID) (WorkspaceGrant, error) {
	store, err := s.workspaceStore(ctx, workspace)
	if err != nil {
		return WorkspaceGrant{}, err
	}
	grant, err := s.grant(ctx, host, store, uuid.UUID(workspace), "")
	if err != nil {
		return WorkspaceGrant{}, err
	}
	key, err := s.peerKey(ctx, workspace)
	if err != nil {
		return WorkspaceGrant{}, err
	}
	return WorkspaceGrant{Grant: grant, PeerKey: key}, nil
}

// peerKey returns the workspace's peer key, making it the first time: every
// server proposes a random one, and the row keeps the first written. Later
// grants only read it.
func (s *Storage) peerKey(ctx context.Context, workspace identity.WorkspaceID) ([]byte, error) {
	key, err := s.queries.ReadWorkspacePeerKey(ctx, uuid.UUID(workspace))
	if err == nil && len(key) == 0 {
		proposed := make([]byte, 32)
		if _, err := rand.Read(proposed); err != nil {
			return nil, fmt.Errorf("make peer key: %w", err)
		}
		key, err = s.queries.WorkspacePeerKey(ctx, WorkspacePeerKeyParams{Key: proposed, WorkspaceID: uuid.UUID(workspace)})
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("read peer key: %w", err)
	}
	return key, nil
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
	o := store.client.Options()
	location, err := locate(aws.ToString(o.BaseEndpoint), store.region, store.name, o.UsePathStyle)
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

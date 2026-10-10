package storage

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// GrantDiskRead issues the host holding disk under the lease a credential
// that reads the disk's objects, its generations' indexes and frames, and
// nothing else. The host's snapshotter serves the disk's generations with
// it. Garage keys cannot be scoped to a prefix, so there it reads the
// workspace bucket. The object store's refusal is a *StoreRefusedError.
func (s *Storage) GrantDiskRead(ctx context.Context, host compute.HostID, container, disk uuid.UUID, token []byte) (Grant, error) {
	var row LockLeasedDiskRow
	err := s.withDiskLease(ctx, host, container, disk, token, func(_ *Queries, locked LockLeasedDiskRow) error {
		row = locked
		return nil
	})
	if err != nil {
		return Grant{}, fmt.Errorf("grant disk reads: %w", err)
	}
	store, ok, err := s.storeAt(row.Bucket, row.Region, row.ConnectionID)
	if err != nil {
		return Grant{}, err
	}
	if !ok {
		return Grant{}, invalid("disk %s has no workspace bucket", disk)
	}
	provider, err := s.providerOf(store)
	if err != nil {
		return Grant{}, err
	}
	grant, revocable, err := provider.issueRead(ctx, store.name, diskPrefix(disk), "lazycloud-disk-"+host.String(), grantLifetime)
	if err != nil {
		return Grant{}, storeError(fmt.Errorf("issue disk read grant: %w", err))
	}
	if revocable {
		if err := s.queries.InsertStorageGrant(ctx, InsertStorageGrantParams{
			AccessKeyID: grant.AccessKeyID, WorkspaceID: row.WorkspaceID, HostID: uuid.UUID(host), ExpiresAt: grant.ExpiresAt,
		}); err != nil {
			return Grant{}, fmt.Errorf("record storage grant: %w", err)
		}
	}
	grant.Location, err = locate(s.config.Endpoint, store.region, grant.Bucket, s.config.Endpoint != "")
	if err != nil {
		return Grant{}, fmt.Errorf("locate workspace bucket: %w", err)
	}
	return grant, nil
}

func (g *garageBuckets) issueRead(ctx context.Context, bucket, _, name string, lifetime time.Duration) (Grant, bool, error) {
	return g.issueWith(ctx, bucket, name, lifetime, garagePerms{Read: true})
}

func (a *awsBuckets) issueRead(ctx context.Context, bucket, prefix, name string, lifetime time.Duration) (Grant, bool, error) {
	policy, err := json.Marshal(readPolicy(bucket, prefix))
	if err != nil {
		return Grant{}, false, fmt.Errorf("encode session policy: %w", err)
	}
	return a.issueWith(ctx, bucket, name, string(policy), lifetime)
}

// readPolicy is the STS session policy of a disk read grant: object reads
// under prefix and nothing else.
func readPolicy(bucket, prefix string) map[string]any {
	return map[string]any{
		"Version": "2012-10-17",
		"Statement": []map[string]any{{
			"Effect":   "Allow",
			"Action":   []string{"s3:GetObject"},
			"Resource": []string{"arn:aws:s3:::" + bucket + "/" + prefix + "*"},
		}},
	}
}

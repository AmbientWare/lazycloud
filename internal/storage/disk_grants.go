package storage

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
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
	if row.Bucket == nil || row.Region == nil {
		return Grant{}, invalid("disk %s has no workspace bucket", disk)
	}
	store, err := s.storeOf(ctx, *row.Bucket, *row.Region, row.ConnectionID)
	if err != nil {
		return Grant{}, err
	}
	provider, err := s.providerOf(ctx, store)
	if err != nil {
		return Grant{}, err
	}
	creds, revocable, err := provider.issueRead(ctx, store.name, diskPrefix(disk), "lazycloud-disk-"+host.String(), grantLifetime)
	if err != nil {
		return Grant{}, storeError(fmt.Errorf("issue disk read grant: %w", err))
	}
	if revocable {
		if err := s.queries.InsertStorageGrant(ctx, InsertStorageGrantParams{
			AccessKeyID: creds.AccessKeyID, WorkspaceID: row.WorkspaceID, HostID: uuid.UUID(host), ExpiresAt: creds.Expires,
		}); err != nil {
			return Grant{}, fmt.Errorf("record storage grant: %w", err)
		}
	}
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

func (g *garageBuckets) issueRead(ctx context.Context, bucket, _, name string, lifetime time.Duration) (aws.Credentials, bool, error) {
	return g.issueWith(ctx, bucket, name, lifetime, garagePerms{Read: true})
}

func (a *awsBuckets) issueRead(ctx context.Context, bucket, prefix, name string, lifetime time.Duration) (aws.Credentials, bool, error) {
	policy, err := json.Marshal(readPolicy(bucket, prefix, a.account))
	if err != nil {
		return aws.Credentials{}, false, fmt.Errorf("encode session policy: %w", err)
	}
	return a.issueWith(ctx, bucket, name, string(policy), lifetime)
}

// readPolicy is the STS session policy of a disk read grant: object reads
// under prefix of account's bucket and nothing else.
func readPolicy(bucket, prefix, account string) map[string]any {
	return map[string]any{
		"Version": "2012-10-17",
		"Statement": []map[string]any{{
			"Effect":    "Allow",
			"Action":    []string{"s3:GetObject"},
			"Resource":  []string{"arn:aws:s3:::" + bucket + "/" + prefix + "*"},
			"Condition": map[string]any{"StringEquals": map[string]any{"s3:ResourceAccount": account}},
		}},
	}
}

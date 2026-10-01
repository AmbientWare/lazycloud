package hostsession

import (
	"context"
	"errors"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// grantRefresh is how long before expiry a host gets a fresh storage grant.
const grantRefresh = 30 * time.Minute

// volumeMounts records the container's volume mounts and returns them as the
// host sees them.
func (s *Server) volumeMounts(ctx context.Context, start execution.StartCommand) ([]*hostproto.VolumeMount, error) {
	if start.Spec.Volumes == nil || len(*start.Spec.Volumes) == 0 {
		return nil, nil
	}
	mounts, err := s.storage.MountVolumes(ctx, start.Workspace, uuid.UUID(start.Container), *start.Spec.Volumes)
	if err != nil {
		return nil, err
	}
	out := make([]*hostproto.VolumeMount, len(mounts))
	for n, m := range mounts {
		mount := &hostproto.VolumeMount{MountPath: m.MountPath, ReadOnly: m.ReadOnly}
		if m.Volume != nil {
			mount.Source = &hostproto.VolumeMount_Volume{Volume: &hostproto.PlatformVolume{
				VolumeId: m.Volume.String(), WorkspaceId: start.Workspace.String(), Prefix: m.Prefix,
			}}
		} else {
			// Validation refuses cloud buckets until workspace secrets can
			// supply their keys.
			b := m.CloudBucket
			mount.Source = &hostproto.VolumeMount_CloudBucket{CloudBucket: &hostproto.CloudBucket{
				Bucket: b.Bucket, Prefix: deref(b.Prefix), Region: deref(b.Region), Endpoint: deref(b.Endpoint),
				ForcePathStyle: b.ForcePathStyle != nil && *b.ForcePathStyle,
			}}
		}
		out[n] = mount
	}
	return out, nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// usesWorkspaceBucket reports whether a start mounts platform volumes or
// disks, which need the workspace's storage grant.
func usesWorkspaceBucket(start *hostproto.StartContainer) bool {
	if len(start.GetDisks()) > 0 {
		return true
	}
	for _, v := range start.GetVolumes() {
		if v.GetVolume() != nil {
			return true
		}
	}
	return false
}

// ensureGrant sends the host a storage grant for workspace unless this
// session sent one that is not close to expiry. Failing to issue one is
// logged and retried at the next sync: the host keeps using a grant it has,
// and a container waiting for its first grant fails to start in time.
func (sess *session) ensureGrant(ctx context.Context, workspace identity.WorkspaceID) error {
	if expires, ok := sess.grants[workspace]; ok && time.Until(expires) > grantRefresh {
		return nil
	}
	grant, err := sess.server.storage.HostGrant(ctx, sess.host, workspace)
	if err != nil {
		if ctx.Err() != nil {
			return status.FromContextError(ctx.Err()).Err()
		}
		sess.server.logger.WarnContext(ctx, "issuing a storage grant failed", "host", sess.host.String(), "workspace", workspace.String(), "error", err)
		return nil
	}
	msg := &hostproto.ServerMessage{
		CommandId: "grant:" + workspace.String() + ":" + grant.ExpiresAt.Format(time.RFC3339),
		Body: &hostproto.ServerMessage_StorageGrant{StorageGrant: &hostproto.StorageGrant{
			WorkspaceId: workspace.String(), Endpoint: grant.Endpoint, Region: grant.Region, Bucket: grant.Bucket,
			AccessKeyId: grant.AccessKeyID, SecretAccessKey: grant.SecretAccessKey, SessionToken: grant.SessionToken,
			ExpiresAt: timestamppb.New(grant.ExpiresAt),
		}},
	}
	if err := sess.stream.Send(msg); err != nil {
		return err //nolint:wrapcheck // The stream's status ends the session.
	}
	sess.grants[workspace] = grant.ExpiresAt
	return nil
}

// refreshGrants keeps a fresh grant on the host for every workspace whose
// volumes or disks its live containers use, and forgets the others.
func (sess *session) refreshGrants(ctx context.Context) error {
	workspaces, err := sess.server.storage.HostMountWorkspaces(ctx, sess.host)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	live := map[identity.WorkspaceID]bool{}
	for _, ws := range workspaces {
		live[ws] = true
		if err := sess.ensureGrant(ctx, ws); err != nil {
			return err
		}
	}
	for ws := range sess.grants {
		if !live[ws] {
			delete(sess.grants, ws)
		}
	}
	return nil
}

func parseDiskCall(containerID, diskID string) (uuid.UUID, uuid.UUID, error) {
	container, err := uuid.Parse(containerID)
	if err != nil {
		return uuid.UUID{}, uuid.UUID{}, status.Error(codes.InvalidArgument, "container_id is not a UUID")
	}
	disk, err := uuid.Parse(diskID)
	if err != nil {
		return uuid.UUID{}, uuid.UUID{}, status.Error(codes.InvalidArgument, "disk_id is not a UUID")
	}
	return container, disk, nil
}

func (s *Server) diskError(ctx context.Context, err error) error {
	var held *storage.ConflictError
	switch {
	case errors.Is(err, storage.ErrStaleLease):
		return status.Error(codes.FailedPrecondition, "the container does not hold the disk")
	case errors.As(err, &held):
		return status.Error(codes.Aborted, held.Error())
	}
	var invalid *storage.InvalidError
	if errors.As(err, &invalid) {
		return status.Error(codes.InvalidArgument, invalid.Error())
	}
	return s.grpcError(ctx, err)
}

// AcquireDisk leases a declared disk to a container on the calling host.
func (s *Server) AcquireDisk(ctx context.Context, req *hostproto.AcquireDiskRequest) (*hostproto.AcquireDiskResponse, error) {
	container, err := uuid.Parse(req.GetContainerId())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, "container_id is not a UUID")
	}
	lease, err := s.storage.AcquireDisk(ctx, hostFrom(ctx), container, req.GetName())
	if err != nil {
		return nil, s.diskError(ctx, err)
	}
	out := &hostproto.AcquireDiskResponse{
		DiskId: lease.Disk.String(), WorkspaceId: lease.Workspace.String(), SizeBytes: lease.SizeBytes, LeaseToken: lease.Token,
	}
	for _, g := range lease.Chain {
		out.Chain = append(out.Chain, &hostproto.DiskGeneration{Generation: g.Generation, ManifestKey: g.ManifestKey, ManifestSha256: g.ManifestSHA256})
	}
	return out, nil
}

// RecordDiskGeneration records a published generation under the lease.
func (s *Server) RecordDiskGeneration(ctx context.Context, req *hostproto.RecordDiskGenerationRequest) (*hostproto.RecordDiskGenerationResponse, error) {
	container, disk, err := parseDiskCall(req.GetContainerId(), req.GetDiskId())
	if err != nil {
		return nil, err
	}
	if err := s.storage.RecordDiskGeneration(ctx, hostFrom(ctx), container, disk, req.GetLeaseToken(), storage.PublishedGeneration{
		Generation: req.GetGeneration(), ParentGeneration: req.GetParentGeneration(),
		ManifestKey: req.GetManifestKey(), ManifestSHA256: req.GetManifestSha256(), AddedBytes: req.GetAddedBytes(), Flat: req.GetFlat(),
	}); err != nil {
		return nil, s.diskError(ctx, err)
	}
	return &hostproto.RecordDiskGenerationResponse{}, nil
}

// RecordDiskCollection records the bytes a collection removed.
func (s *Server) RecordDiskCollection(ctx context.Context, req *hostproto.RecordDiskCollectionRequest) (*hostproto.RecordDiskCollectionResponse, error) {
	container, disk, err := parseDiskCall(req.GetContainerId(), req.GetDiskId())
	if err != nil {
		return nil, err
	}
	if err := s.storage.RecordDiskCollection(ctx, hostFrom(ctx), container, disk, req.GetLeaseToken(), req.GetRemovedBytes(), req.GetBaseGeneration()); err != nil {
		return nil, s.diskError(ctx, err)
	}
	return &hostproto.RecordDiskCollectionResponse{}, nil
}

// ReleaseDisk ends a lease.
func (s *Server) ReleaseDisk(ctx context.Context, req *hostproto.ReleaseDiskRequest) (*hostproto.ReleaseDiskResponse, error) {
	container, disk, err := parseDiskCall(req.GetContainerId(), req.GetDiskId())
	if err != nil {
		return nil, err
	}
	if err := s.storage.ReleaseDisk(ctx, container, disk, req.GetLeaseToken()); err != nil {
		return nil, s.diskError(ctx, err)
	}
	return &hostproto.ReleaseDiskResponse{}, nil
}

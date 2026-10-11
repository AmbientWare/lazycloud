package hostsession

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

const (
	// grantRefresh is how long before expiry a host gets a fresh storage
	// grant.
	grantRefresh = 30 * time.Minute
	// grantAckWait is how long a host may take to confirm it stored a
	// grant; a later sync sends it again.
	grantAckWait = 2 * time.Second
)

// storageRefusal is storage a start needs that the storage owner refused:
// a cloud bucket or the workspace's storage grant. The container fails
// with reason; the host's other containers and its session are unaffected.
type storageRefusal struct {
	reason string
	err    error
}

func (e *storageRefusal) Error() string { return e.err.Error() }

func (e *storageRefusal) Unwrap() error { return e.err }

// refusal returns err as a *storageRefusal of what, such as "storage grant"
// or "cloud bucket at /data", when it is one of the storage owner's typed
// refusals, with the cause its owner is shown. Any other error, such as the
// database's or the object store's failure, is returned as it is, so the
// start is tried again.
func refusal(what string, err error) error {
	var (
		invalid  *storage.InvalidError
		conflict *storage.ConflictError
		refused  *storage.StoreRefusedError
		cause    string
	)
	switch {
	case errors.Is(err, storage.ErrBucketsUnconfigured):
		cause = storage.ErrBucketsUnconfigured.Error()
	case errors.Is(err, storage.ErrNoRegion):
		cause = storage.ErrNoRegion.Error()
	case errors.As(err, &invalid):
		cause = invalid.Reason
	case errors.As(err, &conflict):
		cause = conflict.Reason
	case errors.As(err, &refused):
		cause = "the object store refused it (" + refused.Code + ")"
	default:
		return err
	}
	return &storageRefusal{reason: what + " unavailable: " + cause, err: fmt.Errorf("%s: %w", what, err)}
}

// bucketKeys names the workspace secrets holding the keys of the spec's
// cloud buckets.
func bucketKeys(spec apitypes.WorkloadSpec) []string {
	var names []string
	if spec.Volumes != nil {
		for _, v := range *spec.Volumes {
			if b := v.CloudBucket; b != nil {
				names = append(names, b.AccessKeySecret, b.SecretKeySecret)
			}
		}
	}
	return names
}

// volumeMounts records the container's volume mounts and returns them as the
// host sees them, each cloud bucket with its keys from secrets.
func (s *Server) volumeMounts(ctx context.Context, start execution.StartCommand, secrets map[string]string) ([]*hostproto.VolumeMount, error) {
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
			b := m.CloudBucket
			loc, err := storage.CloudBucketLocation(*b)
			if err != nil {
				return nil, refusal("cloud bucket at "+m.MountPath, err)
			}
			mount.Source = &hostproto.VolumeMount_CloudBucket{CloudBucket: &hostproto.CloudBucket{
				Bucket: loc.Bucket, Prefix: deref(b.Prefix), Region: loc.Region, Endpoint: loc.Endpoint,
				ForcePathStyle: loc.PathStyle, AccessKeyId: secrets[b.AccessKeySecret], SecretAccessKey: secrets[b.SecretKeySecret],
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

// hostGrant is the storage grant this session sent the host for one
// workspace, and whether the host confirmed it stored it.
type hostGrant struct {
	msg     *hostproto.ServerMessage
	expires time.Time
	sentAt  time.Time
	acked   bool
}

// held reports whether the host holds the grant and may use it now.
func (g *hostGrant) held() bool {
	return g != nil && g.acked && time.Now().Before(g.expires)
}

// ensureGrant keeps a storage grant for workspace on the host. A grant the
// host has not confirmed within grantAckWait is sent again, and one close
// to expiry is replaced. A storage refusal while the host holds no grant
// is a *storageRefusal. Any other failure to issue one is logged and tried
// again at the next sync: the host keeps using a grant it holds, and a
// start waiting for its first grant fails if none arrives in time.
func (sess *session) ensureGrant(ctx context.Context, workspace identity.WorkspaceID) error {
	g := sess.grants[workspace]
	if g != nil && time.Until(g.expires) > grantRefresh {
		if g.acked || time.Since(g.sentAt) < grantAckWait {
			return nil
		}
		return sess.sendGrant(workspace, g)
	}
	grant, err := sess.server.storage.HostGrant(ctx, sess.host, workspace)
	if err != nil {
		if ctx.Err() != nil {
			return status.FromContextError(ctx.Err()).Err()
		}
		sess.server.logger.WarnContext(ctx, "issuing a storage grant failed", "host", sess.host.String(), "workspace", workspace.String(), "error", err)
		var refused *storageRefusal
		if err := refusal("storage grant", err); errors.As(err, &refused) && !g.held() {
			return refused
		}
		return nil
	}
	return sess.sendGrant(workspace, &hostGrant{
		expires: grant.ExpiresAt,
		msg: &hostproto.ServerMessage{
			CommandId: "grant:" + workspace.String() + ":" + grant.ExpiresAt.Format(time.RFC3339),
			Body: &hostproto.ServerMessage_StorageGrant{StorageGrant: &hostproto.StorageGrant{
				WorkspaceId: workspace.String(), Endpoint: grant.Endpoint, Region: grant.Region, Bucket: grant.Bucket, ForcePathStyle: grant.PathStyle,
				AccessKeyId: grant.AccessKeyID, SecretAccessKey: grant.SecretAccessKey, SessionToken: grant.SessionToken,
				ExpiresAt: timestamppb.New(grant.ExpiresAt),
			}},
		},
	})
}

func (sess *session) sendGrant(workspace identity.WorkspaceID, g *hostGrant) error {
	if err := sess.stream.Send(g.msg); err != nil {
		return err //nolint:wrapcheck // The stream's status ends the session.
	}
	g.sentAt = time.Now()
	sess.grants[workspace] = g
	return nil
}

// grantAcked records that the host stored the grant command names.
func (sess *session) grantAcked(command string) {
	for _, g := range sess.grants {
		if g.msg.GetCommandId() == command {
			g.acked = true
		}
	}
}

// refreshGrants keeps a fresh grant on the host for every workspace whose
// volumes or disks its live containers use, and forgets the others. A grant
// that cannot be issued is tried again at the next sync.
func (sess *session) refreshGrants(ctx context.Context) error {
	workspaces, err := sess.server.storage.HostMountWorkspaces(ctx, sess.host)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	live := map[identity.WorkspaceID]bool{}
	for _, ws := range workspaces {
		live[ws] = true
		var refused *storageRefusal
		if err := sess.ensureGrant(ctx, ws); err != nil && !errors.As(err, &refused) {
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

// diskError maps disk refusals to status codes. Plan and payment refusals
// keep their message, which the host reports as the container's start failure.
func (s *Server) diskError(ctx context.Context, err error) error {
	var (
		held    *storage.ConflictError
		invalid *storage.InvalidError
		limit   *billing.LimitError
		unpaid  *billing.PaymentRequiredError
	)
	switch {
	case errors.Is(err, storage.ErrStaleLease):
		return status.Error(codes.FailedPrecondition, "the container does not hold the disk")
	case errors.As(err, &held):
		return status.Error(codes.Aborted, held.Error())
	case errors.As(err, &invalid):
		return status.Error(codes.InvalidArgument, invalid.Error())
	case errors.As(err, &limit):
		return status.Error(codes.FailedPrecondition, limit.Error())
	case errors.As(err, &unpaid):
		return status.Error(codes.FailedPrecondition, unpaid.Error())
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

// CollectDisk deletes a disk's unreachable objects under the lease.
func (s *Server) CollectDisk(ctx context.Context, req *hostproto.CollectDiskRequest) (*hostproto.CollectDiskResponse, error) {
	container, disk, err := parseDiskCall(req.GetContainerId(), req.GetDiskId())
	if err != nil {
		return nil, err
	}
	if err := s.storage.CollectDisk(ctx, hostFrom(ctx), container, disk, req.GetLeaseToken(), req.GetBaseGeneration(), req.GetKeys(), req.GetRemovedBytes()); err != nil {
		return nil, s.diskError(ctx, err)
	}
	return &hostproto.CollectDiskResponse{}, nil
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

// diskOperations maps the host's disk operations to the API's.
var diskOperations = map[hostproto.DiskOperation]apitypes.DiskOperation{ //nolint:gochecknoglobals // constant table
	hostproto.DiskOperation_DISK_OPERATION_PUBLISH: apitypes.DiskOperationPublish,
	hostproto.DiskOperation_DISK_OPERATION_RELEASE: apitypes.DiskOperationRelease,
}

// RecordDiskFailure records or clears the holder's last disk failure.
func (s *Server) RecordDiskFailure(ctx context.Context, req *hostproto.RecordDiskFailureRequest) (*hostproto.RecordDiskFailureResponse, error) {
	container, disk, err := parseDiskCall(req.GetContainerId(), req.GetDiskId())
	if err != nil {
		return nil, err
	}
	var failure *storage.DiskFailure
	if f := req.GetFailure(); f != nil {
		operation, ok := diskOperations[f.GetOperation()]
		if !ok {
			return nil, status.Errorf(codes.InvalidArgument, "unknown disk operation %s", f.GetOperation())
		}
		failure = &storage.DiskFailure{Operation: operation, Message: f.GetMessage()}
	}
	if err := s.storage.RecordDiskFailure(ctx, container, disk, req.GetLeaseToken(), failure); err != nil {
		return nil, s.diskError(ctx, err)
	}
	return &hostproto.RecordDiskFailureResponse{}, nil
}

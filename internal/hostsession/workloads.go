package hostsession

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// podWorkload is what a pod, devbox, sandbox or shell container runs.
func (s *Server) podWorkload(ctx context.Context, start execution.StartCommand) (*hostproto.PodWorkload, error) {
	spec := start.Spec
	out := &hostproto.PodWorkload{
		Network: &hostproto.NetworkPolicy{Block: start.Network.Block, Allow: start.Network.Allow},
	}
	if start.Purpose == execution.PurposeShell {
		// A shell container of any release runs nothing of its own.
		out.Idle = true
		return out, nil
	}
	p := spec.Pod
	if p == nil {
		return nil, fmt.Errorf("container %s of a %s has no pod definition", start.Container, start.Kind)
	}
	out.Command = start.Command
	if len(out.Command) == 0 && p.Command != nil {
		out.Command = *p.Command
	}
	out.Devbox = p.Kind == apitypes.PodKindDevbox
	out.Idle = out.Devbox && len(out.Command) == 0
	// A pod is ready once its first port answers. A sandbox is ready once
	// its command started: its ports are for what runs in it later.
	if p.Kind != apitypes.PodKindSandbox {
		for _, port := range execution.PodPorts(spec) {
			out.Ports = append(out.Ports, int32(port)) //nolint:gosec // The schema bounds ports.
		}
	}
	if h := p.HealthCheck; h != nil && h.Port != nil {
		out.Health = &hostproto.HealthCheck{Path: h.Path, Port: int32(*h.Port)} //nolint:gosec // The schema bounds ports.
	}
	if execution.ServesSSH(spec) {
		if s.config.SSH == nil {
			return nil, errors.New("this server holds no SSH keys")
		}
		hostKey, _, err := s.config.SSH.HostKey(ctx, start.Workload)
		if err != nil {
			return nil, err
		}
		_, authority, err := s.config.SSH.Authority(ctx, start.Workspace)
		if err != nil {
			return nil, err
		}
		out.Ssh = &hostproto.SshServer{HostKey: hostKey, UserAuthority: authority}
	}
	return out, nil
}

// restoreOut is the snapshot a start restores, with a URL to fetch it.
func (s *Server) restoreOut(ctx context.Context, container execution.ContainerID) (*hostproto.SnapshotRestore, error) {
	restore, err := s.execution.RestoreFor(ctx, container)
	if err != nil || restore == nil {
		return nil, err
	}
	url, err := s.storage.SnapshotDownloadURL(ctx, restore.Workspace, restore.Snapshot)
	if err != nil {
		return nil, err
	}
	return &hostproto.SnapshotRestore{
		SnapshotId: restore.Snapshot.String(), Url: url, Sha256: restore.SHA256, Automatic: restore.Automatic,
	}, nil
}

func disksOut(spec apitypes.WorkloadSpec) []*hostproto.DiskAttachment {
	if spec.Disks == nil {
		return nil
	}
	out := make([]*hostproto.DiskAttachment, len(*spec.Disks))
	for n, d := range *spec.Disks {
		out[n] = &hostproto.DiskAttachment{Name: d.Name, MountPath: d.MountPath}
	}
	return out
}

// syncWorkloads sends the snapshot, publish and network commands durable
// state implies that this session has not sent.
func (sess *session) syncWorkloads(ctx context.Context, commands execution.HostCommands, derived map[string]bool) error {
	for _, snap := range commands.Snapshot {
		id := "snapshot:" + snap.Snapshot.String()
		derived[id] = true
		if sess.sent[id] {
			continue
		}
		url, err := sess.server.storage.SnapshotUploadURL(ctx, snap.Workspace, snap.Snapshot)
		if err != nil {
			return sess.server.grpcError(ctx, err)
		}
		msg := &hostproto.SnapshotContainer{
			ContainerId: snap.Container.String(), SnapshotId: snap.Snapshot.String(), UploadUrl: url,
			Deadline: timestamppb.New(snap.Deadline), Traceparent: snap.Traceparent,
		}
		if r := snap.Ready; r != nil {
			msg.Ready = &hostproto.ReadinessProbe{
				Path: *r.ReadinessPath, Port: int32(*r.ReadinessPort), //nolint:gosec // The schema bounds ports.
				TimeoutSeconds:  int32(valueOr(r.ReadinessTimeoutSeconds, 600)), //nolint:gosec // The schema bounds timeouts.
				IntervalSeconds: valueOr(r.ReadinessIntervalSeconds, 1),
			}
		}
		if err := sess.send(&hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_Snapshot{Snapshot: msg}}); err != nil {
			return err
		}
	}
	for _, pub := range commands.Publish {
		id := "publish:" + pub.Request.String()
		derived[id] = true
		if sess.sent[id] || sess.server.images == nil {
			continue
		}
		repository, insecure, auth, err := sess.server.images.FilesystemTarget(ctx, pub.Workspace, pub.Deadline)
		if err != nil {
			return err
		}
		msg := &hostproto.PublishFilesystem{
			ContainerId: pub.Container.String(), RequestId: pub.Request.String(), Repository: repository,
			InsecureRegistry: insecure, RegistryAuth: registryAuthOut(auth), Deadline: timestamppb.New(pub.Deadline),
		}
		if err := sess.send(&hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_PublishFilesystem{PublishFilesystem: msg}}); err != nil {
			return err
		}
	}
	for _, n := range commands.Network {
		id := "network:" + n.Container.String() + ":" + strconv.Itoa(n.Version)
		derived[id] = true
		if sess.sent[id] {
			continue
		}
		msg := &hostproto.UpdateNetwork{
			ContainerId: n.Container.String(), Policy: &hostproto.NetworkPolicy{Block: n.Policy.Block, Allow: n.Policy.Allow},
			Version: int32(n.Version), //nolint:gosec // Versions count policy changes.
		}
		if err := sess.send(&hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_Network{Network: msg}}); err != nil {
			return err
		}
	}
	return nil
}

func isHexDigest(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, c := range s {
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func valueOr[T any](v *T, def T) T {
	if v == nil {
		return def
	}
	return *v
}

// CompleteSnapshot records how a host's snapshot ended.
func (s *Server) CompleteSnapshot(ctx context.Context, req *hostproto.CompleteSnapshotRequest) (*hostproto.CompleteSnapshotResponse, error) {
	container, err := parseContainer(req.GetContainerId())
	if err != nil {
		return nil, err
	}
	snapshot, err := uuid.Parse(req.GetSnapshotId())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, "snapshot_id is not a UUID")
	}
	outcome := execution.SnapshotOutcome{
		Snapshot: snapshot, Container: container, SizeBytes: req.GetSizeBytes(), SHA256: req.GetSha256(),
		Failure: req.GetFailure(), Unsupported: req.GetUnsupported(),
	}
	if outcome.Failure == "" && (outcome.SizeBytes <= 0 || len(outcome.SHA256) != 64) {
		return nil, status.Error(codes.InvalidArgument, "a stored snapshot needs its size and sha256")
	}
	if outcome.Unsupported && outcome.Failure == "" {
		outcome.Failure = "the host cannot checkpoint containers"
	}
	err = s.execution.CompleteSnapshot(ctx, hostFrom(ctx), outcome)
	if errors.Is(err, execution.ErrStaleSnapshot) {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	if err != nil {
		return nil, s.grpcError(ctx, err)
	}
	return &hostproto.CompleteSnapshotResponse{}, nil
}

// CompleteFilesystemImage records how a host's filesystem publish ended and
// registers the published image.
func (s *Server) CompleteFilesystemImage(ctx context.Context, req *hostproto.CompleteFilesystemImageRequest) (*hostproto.CompleteFilesystemImageResponse, error) {
	container, err := parseContainer(req.GetContainerId())
	if err != nil {
		return nil, err
	}
	request, err := uuid.Parse(req.GetRequestId())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, "request_id is not a UUID")
	}
	failure := req.GetFailure()
	if failure == "" && req.GetReference() == "" {
		return nil, status.Error(codes.InvalidArgument, "a published image needs its reference")
	}
	route, err := s.execution.Route(ctx, container)
	if err != nil {
		return nil, s.grpcError(ctx, err)
	}
	python := string(route.Spec.Image.PythonVersion)
	architecture := req.GetArchitecture()
	if architecture == "" {
		architecture = "amd64"
	}
	err = s.execution.FinishFilesystemImage(ctx, hostFrom(ctx), container, request, failure, func(ws identity.WorkspaceID) (string, error) {
		// The host names only a digest in the repository this workspace's
		// images go to.
		repository := s.images.FilesystemRepository(ws)
		digest, ok := strings.CutPrefix(req.GetReference(), repository+"@sha256:")
		if !ok || !isHexDigest(digest) {
			return "", fmt.Errorf("the host reported %q, not an image in %s", req.GetReference(), repository)
		}
		return s.images.RegisterFilesystem(ctx, ws, req.GetReference(), architecture, python)
	})
	if errors.Is(err, execution.ErrStaleSnapshot) {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	if err != nil {
		return nil, s.grpcError(ctx, err)
	}
	return &hostproto.CompleteFilesystemImageResponse{}, nil
}

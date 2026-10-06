package hostsession

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// errImageUnpinned means a release names an image without the reference
// it pinned at deploy.
var errImageUnpinned = errors.New("the image has no pinned reference")

// maxBuildLogLine bounds one stored line of build output.
const maxBuildLogLine = 16 << 10

// imagePull is how host pulls the image of a container of workspace: the
// image by digest its release pinned, or the platform's image for its
// Python version. Either waits while the image converts: the first with
// images.BuildWaitError, the second with images.PlatformWaitError.
func (s *Server) imagePull(ctx context.Context, host compute.HostID, workspace identity.WorkspaceID, spec apitypes.ImageSpec) (images.Pull, error) {
	if spec.ImageId == nil {
		return s.images.ManagedPull(ctx, host, string(spec.PythonVersion))
	}
	if spec.Reference == nil {
		return images.Pull{}, fmt.Errorf("release names image %s: %w", *spec.ImageId, errImageUnpinned)
	}
	return s.images.ConvertedPull(ctx, workspace, *spec.ImageId, *spec.Reference)
}

func registryAuthOut(auth *images.Auth) *hostproto.RegistryAuth {
	if auth == nil {
		return nil
	}
	return &hostproto.RegistryAuth{Username: auth.Username, Password: auth.Password, IdentityToken: auth.IdentityToken}
}

// syncBuilds sends start commands for the build containers starting on the
// host that this session has not sent yet, and records them in derived.
func (sess *session) syncBuilds(ctx context.Context, derived map[string]bool) error {
	starts, err := sess.server.execution.BuildStarts(ctx, sess.host)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	for _, start := range starts {
		id := "start:" + start.Container.String()
		derived[id] = true
		if sess.sent[id] {
			continue
		}
		msg, err := sess.server.buildStartMessage(ctx, sess.host, id, start)
		if errors.Is(err, images.ErrStaleBuild) {
			// The build failed before it could start; its container stops.
			continue
		}
		if err != nil {
			return sess.server.grpcError(ctx, err)
		}
		if err := sess.send(msg); err != nil {
			return err
		}
	}
	return nil
}

func (s *Server) buildStartMessage(ctx context.Context, host compute.HostID, id string, start execution.BuildStart) (*hostproto.ServerMessage, error) {
	command, err := s.images.BuildCommandOf(ctx, host, start)
	if err != nil {
		return nil, err
	}
	build := &hostproto.ImageBuild{
		BuildId:          command.Build.String(),
		Attempt:          int32(command.Attempt), //nolint:gosec // At most two attempts.
		Dockerfile:       command.Dockerfile,
		Platform:         command.Platform,
		PushRepository:   command.PushRepository,
		CacheRef:         command.CacheRef,
		InsecureRegistry: command.Insecure,
		RegistryAuth:     map[string]*hostproto.RegistryAuth{},
		Deadline:         timestamppb.New(command.Deadline),
		Secrets:          command.Secrets,
	}
	for host, auth := range command.Auth {
		build.RegistryAuth[host] = registryAuthOut(&auth)
	}
	if command.Context != nil {
		var digest storage.Digest
		copy(digest[:], command.Context)
		url, expires, err := s.storage.SourceURL(ctx, command.ContextWorkspace, digest)
		if err != nil {
			return nil, err
		}
		build.Context = &hostproto.Source{Sha256: digest.String(), Url: url, UrlExpiresAt: timestamppb.New(expires)}
	}
	reserved, memory, memoryLimit := images.BuildResources()
	return &hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_Start{Start: &hostproto.StartContainer{
		ContainerId: start.Container.String(),
		Resources: &hostproto.Resources{
			CpuMillis: int64(reserved), MemoryBytes: memory, MemoryLimitBytes: memoryLimit,
			GpuCount: int32(command.GPUs), //nolint:gosec // A build holds at most one GPU.
		},
		Build: build,
	}}}, nil
}

// CompleteImageBuild records a build container's outcome.
func (s *Server) CompleteImageBuild(ctx context.Context, req *hostproto.CompleteImageBuildRequest) (*hostproto.CompleteImageBuildResponse, error) {
	container, err := parseContainer(req.GetContainerId())
	if err != nil {
		return nil, err
	}
	var outcome images.BuildOutcome
	switch o := req.GetOutcome().(type) {
	case *hostproto.CompleteImageBuildRequest_Digest:
		outcome.Digest = o.Digest
	case *hostproto.CompleteImageBuildRequest_Failure:
		outcome.Failure = o.Failure
		if outcome.Failure == "" {
			outcome.Failure = "the build failed"
		}
	default:
		return nil, status.Error(codes.InvalidArgument, "an outcome is required")
	}
	outcome.Transient = req.GetFailureTransient()
	for _, c := range req.GetConvertedLayers() {
		outcome.Converted = append(outcome.Converted, images.ConvertedLayer{Blob: c.GetBlobDigest(), DataBytes: c.GetDataBytes(), IndexBytes: c.GetIndexBytes()})
	}
	for _, u := range req.GetUploadedLayers() {
		outcome.Uploaded = append(outcome.Uploaded, images.UploadedLayer{Blob: u.GetBlobDigest(), ETags: u.GetPartEtags()})
	}
	uploads, err := s.images.CompleteBuild(ctx, hostFrom(ctx), container, outcome)
	var invalid *images.InvalidError
	if errors.As(err, &invalid) {
		return nil, status.Error(codes.InvalidArgument, invalid.Error())
	}
	if err != nil {
		return nil, s.grpcError(ctx, err)
	}
	resp := &hostproto.CompleteImageBuildResponse{}
	for _, u := range uploads {
		upload := &hostproto.LayerUpload{BlobDigest: u.Blob, DiffId: u.DiffID}
		if u.Index != "" {
			upload.IndexUrl, upload.DataPartUrls, upload.DataPartBytes = u.Index, u.DataParts, u.PartBytes
		}
		resp.LayerUploads = append(resp.LayerUploads, upload)
	}
	return resp, nil
}

// AppendImageBuildLogs stores a build container's output.
func (s *Server) AppendImageBuildLogs(ctx context.Context, req *hostproto.AppendImageBuildLogsRequest) (*hostproto.AppendImageBuildLogsResponse, error) {
	container, err := parseContainer(req.GetContainerId())
	if err != nil {
		return nil, err
	}
	lines := make([]images.LogLine, len(req.GetLines()))
	for n, line := range req.GetLines() {
		data := line.GetData()
		if len(data) > maxBuildLogLine {
			data = data[:maxBuildLogLine]
		}
		at := time.Now()
		if line.GetTime() != nil {
			at = line.GetTime().AsTime()
		}
		lines[n] = images.LogLine{Data: strings.ReplaceAll(strings.ToValidUTF8(data, "�"), "\x00", ""), Time: at}
	}
	if err := s.images.AppendLogs(ctx, hostFrom(ctx), container, lines); err != nil {
		return nil, s.grpcError(ctx, err)
	}
	return &hostproto.AppendImageBuildLogsResponse{}, nil
}

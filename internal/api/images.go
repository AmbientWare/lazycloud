package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
)

// ResolveImage computes a definition's image without building it.
func (s *Server) ResolveImage(ctx context.Context, req ResolveImageRequestObject) (ResolveImageResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	resolution, err := s.owners.Images.Resolve(ctx, ws.ID, *req.Body)
	if err != nil {
		return nil, err
	}
	return ResolveImage200JSONResponse(resolutionOut(resolution)), nil
}

// BuildImage resolves a definition and builds its image unless it is ready.
func (s *Server) BuildImage(ctx context.Context, req BuildImageRequestObject) (BuildImageResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	force := req.Params.Force != nil && *req.Params.Force
	resolution, err := s.owners.Images.Build(ctx, ws.ID, *req.Body, force)
	if err != nil {
		return nil, err
	}
	return BuildImage200JSONResponse(resolutionOut(resolution)), nil
}

// GetImage returns an image the workspace resolved.
func (s *Server) GetImage(ctx context.Context, req GetImageRequestObject) (GetImageResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	image, err := s.owners.Images.Get(ctx, ws.ID, req.Image)
	if err != nil {
		return nil, err
	}
	return GetImage200JSONResponse(imageOut(image)), nil
}

// GetImageBuild reads a build, waiting for it to finish when asked.
func (s *Server) GetImageBuild(ctx context.Context, req GetImageBuildRequestObject) (GetImageBuildResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	wait := time.Duration(0)
	if req.Params.WaitSeconds != nil {
		wait = time.Duration(*req.Params.WaitSeconds) * time.Second
	}
	build, err := s.owners.Images.GetBuild(ctx, s.owners.Listener, ws.ID, req.Build, wait)
	if err != nil {
		return nil, err
	}
	return GetImageBuild200JSONResponse(buildOut(build)), nil
}

// StreamImageBuildLogs writes build output as NDJSON, like task logs.
func (s *Server) StreamImageBuildLogs(ctx context.Context, req StreamImageBuildLogsRequestObject) (StreamImageBuildLogsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	// Report an unknown build as an error before the stream starts.
	if _, err := s.owners.Images.GetBuild(ctx, s.owners.Listener, ws.ID, req.Build, 0); err != nil {
		return nil, err
	}
	stream := buildLogStream{ctx: ctx, server: s, workspace: ws.ID, build: req.Build}
	if req.Params.After != nil {
		stream.after = *req.Params.After
	}
	if req.Params.Follow != nil {
		stream.follow = *req.Params.Follow
	}
	return stream, nil
}

type buildLogStream struct {
	ctx       context.Context //nolint:containedctx // Lives for one response.
	server    *Server
	workspace identity.WorkspaceID
	build     uuid.UUID
	after     int64
	follow    bool
}

func (l buildLogStream) VisitStreamImageBuildLogsResponse(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	flush := http.NewResponseController(w)
	if err := flush.Flush(); err != nil {
		return nil //nolint:nilerr // The client is gone.
	}
	enc := json.NewEncoder(w)
	err := l.server.owners.Images.StreamLogs(l.ctx, l.server.owners.Listener, l.workspace, l.build, l.after, l.follow, logHeartbeat,
		func(batch []images.LogEntry) error {
			if len(batch) == 0 {
				if _, err := w.Write([]byte("\n")); err != nil {
					return fmt.Errorf("write heartbeat: %w", err)
				}
			}
			for _, entry := range batch {
				if err := enc.Encode(apitypes.ImageBuildLogEntry{
					Id: entry.ID, Attempt: entry.Attempt, Data: entry.Data, Time: entry.Time,
				}); err != nil {
					return fmt.Errorf("write build log entry: %w", err)
				}
			}
			if err := flush.Flush(); err != nil {
				return fmt.Errorf("flush build log entries: %w", err)
			}
			return nil
		})
	if err != nil && l.ctx.Err() == nil {
		l.server.logger.WarnContext(l.ctx, "build log stream ended early", "build", l.build.String(), "error", err)
	}
	return nil
}

// checkImages rejects a deployment naming an image the workspace cannot run
// and pins each named image's reference into the request, so the release
// runs exactly that image. A reference the caller sent is replaced.
func (s *Server) checkImages(ctx context.Context, workspace identity.WorkspaceID, req *apitypes.DeploymentRequest) error {
	for n := range req.Functions {
		if err := s.pinImage(ctx, workspace, &req.Functions[n]); err != nil {
			return err
		}
	}
	return nil
}

// pinImage does checkImages for one function spec.
func (s *Server) pinImage(ctx context.Context, workspace identity.WorkspaceID, spec *apitypes.FunctionSpec) error {
	image := &spec.Image
	image.Reference = nil
	if image.ImageId == nil {
		return nil
	}
	reference, err := s.owners.Images.Deployable(ctx, workspace, *image.ImageId, string(image.PythonVersion))
	if err != nil {
		return fmt.Errorf("function %s: %w", spec.Name, err)
	}
	image.Reference = &reference
	return nil
}

func imageOut(i images.Image) apitypes.Image {
	return apitypes.Image{
		Id: i.ID, PythonVersion: i.PythonVersion, Architecture: apitypes.ImageArchitecture(i.Architecture),
		Ready: i.Reference != nil, CreatedAt: i.CreatedAt, ReadyAt: i.ReadyAt,
	}
}

func buildOut(b images.Build) apitypes.ImageBuild {
	out := apitypes.ImageBuild{
		Id: b.ID, ImageId: b.ImageID, Status: apitypes.ImageBuildStatus(b.Status), Phase: apitypes.ImageBuildPhase(b.Phase),
		Attempt: b.Attempt, CreatedAt: b.CreatedAt, FinishedAt: b.FinishedAt,
	}
	if b.Failure != "" {
		out.Failure = &b.Failure
	}
	return out
}

func resolutionOut(r images.Resolution) apitypes.ImageResolution {
	out := apitypes.ImageResolution{Image: imageOut(r.Image)}
	if r.Build != nil {
		b := buildOut(*r.Build)
		out.Build = &b
	}
	return out
}

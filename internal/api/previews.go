package api

import (
	"context"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// errPreviewNotRunning means the preview has no ready container to sync.
var errPreviewNotRunning error = &apitypes.Error{Code: apitypes.Conflict, Message: "the preview has no ready container"}

// renewEvery paces lease renewals while a follower reads a preview's output.
const renewEvery = 15 * time.Second

func (s *server) previewOut(ctx context.Context, p control.Preview) (apitypes.Preview, error) {
	out := apitypes.Preview{
		Id: p.Release, App: p.App, Name: p.Name, Kind: p.Kind, State: apitypes.PreviewStateStarting,
		Url: s.owners.Edge.PreviewURL(p.Release, p.Spec), CreatedAt: p.CreatedAt,
	}
	if p.Live {
		expires := p.LeaseExpiresAt
		if p.DeadlineAt != nil && p.DeadlineAt.Before(expires) {
			expires = *p.DeadlineAt
		}
		out.ExpiresAt = &expires
	}
	c, found, err := s.owners.Execution.NewestContainer(ctx, p.Release)
	if err != nil {
		return apitypes.Preview{}, err
	}
	if found {
		id := uuid.UUID(c.ID)
		out.ContainerId = &id
		if c.State == execution.ContainerReady {
			out.State = apitypes.PreviewStateReady
		}
	}
	// A handler that cannot import ends the preview: no container of it
	// starts again, so serve shows why instead of waiting.
	if p.LoadError != nil {
		reason := apitypes.StopReasonLoadError
		out.StopReason, out.Error = &reason, p.LoadError
	}
	if !p.Live || p.LoadError != nil {
		out.State = apitypes.PreviewStateStopped
	}
	return out, nil
}

// CreatePreview starts a preview of one workload definition.
func (s *server) CreatePreview(ctx context.Context, req CreatePreviewRequestObject) (CreatePreviewResponseObject, error) {
	ws := workspaceFrom(ctx)
	caller, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.pinImage(ctx, ws.ID, &req.Body.Spec); err != nil {
		return nil, err
	}
	p, err := s.owners.Control.CreatePreview(ctx, ws.ID, caller.User, req.App, req.Body.Spec, valueOr(req.Body.TimeoutSeconds, 0))
	if err != nil {
		return nil, err
	}
	out, err := s.previewOut(ctx, p)
	if err != nil {
		return nil, err
	}
	return CreatePreview201JSONResponse(out), nil
}

func (s *server) preview(ctx context.Context, id uuid.UUID) (identity.Workspace, control.Preview, error) {
	ws := workspaceFrom(ctx)
	p, err := s.owners.Control.GetPreview(ctx, ws.ID, id)
	return ws, p, err
}

// GetPreview reads a preview, waiting for its container when asked.
func (s *server) GetPreview(ctx context.Context, req GetPreviewRequestObject) (GetPreviewResponseObject, error) {
	ws, p, err := s.preview(ctx, req.Preview)
	if err != nil {
		return nil, err
	}
	wait := time.Duration(valueOr(req.Params.WaitSeconds, 0)) * time.Second
	// A container becoming ready wakes claims of its release.
	wake, cancel := s.owners.Listener.Subscribe(database.ChannelClaim, p.Release.String())
	defer cancel()
	deadline := time.After(wait)
	poll := time.NewTicker(500 * time.Millisecond)
	defer poll.Stop()
	var renewed time.Time
	for {
		out, err := s.previewOut(ctx, p)
		if err != nil {
			return nil, err
		}
		if out.State != apitypes.PreviewStateStarting || wait == 0 {
			return GetPreview200JSONResponse(out), nil
		}
		// A caller waiting for the container follows the preview, so a slow
		// start (a new host, a large image) does not outlast the lease.
		if time.Since(renewed) >= renewEvery {
			if _, err := s.owners.Control.RenewPreview(ctx, p.Release); err != nil {
				return nil, err
			}
			renewed = time.Now()
		}
		select {
		case <-wake:
		case <-poll.C:
		case <-deadline:
			return GetPreview200JSONResponse(out), nil
		case <-ctx.Done():
			return GetPreview200JSONResponse(out), nil
		}
		if p, err = s.owners.Control.GetPreview(ctx, ws.ID, p.Release); err != nil {
			return nil, err
		}
	}
}

// StopPreview stops a preview and its container.
func (s *server) StopPreview(ctx context.Context, req StopPreviewRequestObject) (StopPreviewResponseObject, error) {
	ws, _, err := s.preview(ctx, req.Preview)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Control.StopPreview(ctx, ws.ID, req.Preview); err != nil {
		return nil, err
	}
	p, err := s.owners.Control.GetPreview(ctx, ws.ID, req.Preview)
	if err != nil {
		return nil, err
	}
	out, err := s.previewOut(ctx, p)
	if err != nil {
		return nil, err
	}
	return StopPreview200JSONResponse(out), nil
}

// SyncPreviewFiles applies a tar of source changes to the preview's
// container.
func (s *server) SyncPreviewFiles(ctx context.Context, req SyncPreviewFilesRequestObject) (SyncPreviewFilesResponseObject, error) {
	_, p, err := s.preview(ctx, req.Preview)
	if err != nil {
		return nil, err
	}
	c, found, err := s.owners.Execution.NewestContainer(ctx, p.Release)
	if err != nil {
		return nil, err
	}
	if !p.Live || !found || c.State != execution.ContainerReady || c.Host == nil {
		return nil, errPreviewNotRunning
	}
	written, removed, err := s.owners.Edge.Sync(ctx, *c.Host, uuid.UUID(c.ID), req.Body)
	if err != nil {
		return nil, err
	}
	return SyncPreviewFiles200JSONResponse{Written: written, Removed: removed}, nil
}

// StreamPreviewOutput writes the preview container's output as NDJSON. A
// follower keeps the preview alive; the stream ends when it stops.
func (s *server) StreamPreviewOutput(ctx context.Context, req StreamPreviewOutputRequestObject) (StreamPreviewOutputResponseObject, error) {
	_, p, err := s.preview(ctx, req.Preview)
	if err != nil {
		return nil, err
	}
	var renewed time.Time
	done := func(ctx context.Context) (bool, error) {
		if time.Since(renewed) < renewEvery {
			return false, nil
		}
		live, err := s.owners.Control.RenewPreview(ctx, p.Release)
		renewed = time.Now()
		return !live, err
	}
	after, follow := valueOr(req.Params.After, 0), valueOr(req.Params.Follow, false)
	return ndjson(s, ctx, containerLogOut, func(emit func([]execution.ContainerLogEntry) error) error {
		return s.owners.Execution.StreamReleaseLogs(ctx, s.owners.Listener, p.Release, after, follow, logHeartbeat, done, emit)
	}, "preview output stream", "preview", p.Release.String()), nil
}

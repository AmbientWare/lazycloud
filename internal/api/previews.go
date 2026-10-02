package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// errPreviewNotRunning means the preview has no ready container to sync.
var errPreviewNotRunning = errors.New("the preview has no ready container")

// renewEvery paces lease renewals while a follower reads a preview's output.
const renewEvery = 15 * time.Second

func (s *Server) previewOut(ctx context.Context, p control.Preview) (apitypes.Preview, error) {
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
	if !p.Live {
		out.State = apitypes.PreviewStateStopped
	}
	return out, nil
}

// CreatePreview starts a preview of one workload definition.
func (s *Server) CreatePreview(ctx context.Context, req CreatePreviewRequestObject) (CreatePreviewResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	user, err := s.user(ctx)
	if err != nil {
		return nil, err
	}
	timeout := 0
	if req.Body.TimeoutSeconds != nil {
		timeout = *req.Body.TimeoutSeconds
	}
	if err := s.pinImage(ctx, ws.ID, &req.Body.Spec); err != nil {
		return nil, err
	}
	p, err := s.owners.Control.CreatePreview(ctx, ws.ID, user, req.App, req.Body.Spec, timeout)
	if err != nil {
		return nil, err
	}
	out, err := s.previewOut(ctx, p)
	if err != nil {
		return nil, err
	}
	return CreatePreview201JSONResponse(out), nil
}

func (s *Server) preview(ctx context.Context, workspace string, id uuid.UUID) (identity.Workspace, control.Preview, error) {
	ws, err := s.workspace(ctx, workspace)
	if err != nil {
		return identity.Workspace{}, control.Preview{}, err
	}
	p, err := s.owners.Control.GetPreview(ctx, ws.ID, id)
	return ws, p, err
}

// GetPreview reads a preview, waiting for its container when asked.
func (s *Server) GetPreview(ctx context.Context, req GetPreviewRequestObject) (GetPreviewResponseObject, error) {
	ws, p, err := s.preview(ctx, req.Workspace, req.Preview)
	if err != nil {
		return nil, err
	}
	wait := time.Duration(0)
	if req.Params.WaitSeconds != nil {
		wait = time.Duration(*req.Params.WaitSeconds) * time.Second
	}
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
func (s *Server) StopPreview(ctx context.Context, req StopPreviewRequestObject) (StopPreviewResponseObject, error) {
	ws, _, err := s.preview(ctx, req.Workspace, req.Preview)
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
func (s *Server) SyncPreviewFiles(ctx context.Context, req SyncPreviewFilesRequestObject) (SyncPreviewFilesResponseObject, error) {
	_, p, err := s.preview(ctx, req.Workspace, req.Preview)
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
func (s *Server) StreamPreviewOutput(ctx context.Context, req StreamPreviewOutputRequestObject) (StreamPreviewOutputResponseObject, error) {
	_, p, err := s.preview(ctx, req.Workspace, req.Preview)
	if err != nil {
		return nil, err
	}
	stream := previewOutput{ctx: ctx, server: s, release: p.Release}
	if req.Params.After != nil {
		stream.after = *req.Params.After
	}
	if req.Params.Follow != nil {
		stream.follow = *req.Params.Follow
	}
	return stream, nil
}

type previewOutput struct {
	// ctx is the request's context; the generated visitor does not pass one.
	ctx     context.Context //nolint:containedctx // Lives for one response.
	server  *Server
	release uuid.UUID
	after   int64
	follow  bool
}

func (o previewOutput) VisitStreamPreviewOutputResponse(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	flush := http.NewResponseController(w)
	if err := flush.Flush(); err != nil {
		return nil //nolint:nilerr // The client is gone.
	}
	enc := json.NewEncoder(w)
	var renewed time.Time
	done := func(ctx context.Context) (bool, error) {
		if time.Since(renewed) < renewEvery {
			return false, nil
		}
		live, err := o.server.owners.Control.RenewPreview(ctx, o.release)
		renewed = time.Now()
		return !live, err
	}
	err := o.server.owners.Execution.StreamReleaseLogs(o.ctx, o.server.owners.Listener, o.release, o.after, o.follow, logHeartbeat, done,
		func(batch []execution.ContainerLogEntry) error {
			if len(batch) == 0 {
				if _, err := w.Write([]byte("\n")); err != nil {
					return fmt.Errorf("write heartbeat: %w", err)
				}
			}
			for _, entry := range batch {
				if err := enc.Encode(apitypes.ContainerLogEntry{
					Id: entry.ID, Stream: apitypes.ContainerLogEntryStream(entry.Stream), Data: entry.Data, Time: entry.Time,
				}); err != nil {
					return fmt.Errorf("write log entry: %w", err)
				}
			}
			if err := flush.Flush(); err != nil {
				return fmt.Errorf("flush log entries: %w", err)
			}
			return nil
		})
	if err != nil && o.ctx.Err() == nil {
		o.server.logger.WarnContext(o.ctx, "preview output stream ended early", "preview", o.release.String(), "error", err)
	}
	return nil
}

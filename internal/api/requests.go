package api

import (
	"context"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// ListHttpRequests lists an app's endpoint and ASGI requests, newest first.
func (s *Server) ListHttpRequests(ctx context.Context, req ListHttpRequestsRequestObject) (ListHttpRequestsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	limit := 50
	if req.Params.Limit != nil {
		limit = *req.Params.Limit
	}
	requests, next, err := s.owners.Edge.ListRequests(ctx, ws.ID, req.App, req.Params.Name, req.Params.Before, limit)
	if err != nil {
		return nil, err
	}
	return ListHttpRequests200JSONResponse{Data: requests, Next: next}, nil
}

// GetHttpRequest returns one request by its X-Request-Id.
func (s *Server) GetHttpRequest(ctx context.Context, req GetHttpRequestRequestObject) (GetHttpRequestResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	out, err := s.owners.Edge.Request(ctx, ws.ID, req.HttpRequest)
	if err != nil {
		return nil, err
	}
	return GetHttpRequest200JSONResponse(out), nil
}

// ListHttpRequestLogs returns what the workload wrote while serving the
// request.
func (s *Server) ListHttpRequestLogs(ctx context.Context, req ListHttpRequestLogsRequestObject) (ListHttpRequestLogsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	var after int64
	if req.Params.After != nil {
		after = *req.Params.After
	}
	limit := 1000
	if req.Params.Limit != nil {
		limit = *req.Params.Limit
	}
	entries, err := s.owners.Execution.RequestLogs(ctx, uuid.UUID(ws.ID), req.HttpRequest, after, limit)
	if err != nil {
		return nil, err
	}
	out := ListHttpRequestLogs200JSONResponse{Data: make([]apitypes.ContainerLogEntry, len(entries))}
	for n, e := range entries {
		out.Data[n] = apitypes.ContainerLogEntry{Id: e.ID, Stream: apitypes.ContainerLogEntryStream(e.Stream), Data: e.Data, Time: e.Time}
	}
	return out, nil
}

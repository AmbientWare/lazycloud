package api

import (
	"context"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// ListHttpRequests lists an app's endpoint and ASGI requests, newest first.
func (s *server) ListHttpRequests(ctx context.Context, req ListHttpRequestsRequestObject) (ListHttpRequestsResponseObject, error) {
	ws := workspaceFrom(ctx)
	requests, next, err := s.owners.Edge.ListRequests(ctx, ws.ID, req.App, req.Params.Name, req.Params.Before, valueOr(req.Params.Limit, 50))
	if err != nil {
		return nil, err
	}
	return ListHttpRequests200JSONResponse{Data: requests, Next: next}, nil
}

// GetHttpRequest returns one request by its X-Request-Id.
func (s *server) GetHttpRequest(ctx context.Context, req GetHttpRequestRequestObject) (GetHttpRequestResponseObject, error) {
	ws := workspaceFrom(ctx)
	out, err := s.owners.Edge.Request(ctx, ws.ID, req.HttpRequest)
	if err != nil {
		return nil, err
	}
	return GetHttpRequest200JSONResponse(out), nil
}

// ListHttpRequestLogs returns what the workload wrote while serving the
// request.
func (s *server) ListHttpRequestLogs(ctx context.Context, req ListHttpRequestLogsRequestObject) (ListHttpRequestLogsResponseObject, error) {
	ws := workspaceFrom(ctx)
	entries, err := s.owners.Execution.RequestLogs(ctx, uuid.UUID(ws.ID), req.HttpRequest, valueOr(req.Params.After, 0), valueOr(req.Params.Limit, 1000))
	if err != nil {
		return nil, err
	}
	out := ListHttpRequestLogs200JSONResponse{Data: make([]apitypes.ContainerLogEntry, len(entries))}
	for n, e := range entries {
		out.Data[n] = containerLogOut(e)
	}
	return out, nil
}

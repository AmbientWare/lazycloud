package api

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/edge"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

type queryKey struct{}

// withQuery keeps the raw query for operations that turn it into
// arguments.
func withQuery(f StrictHandlerFunc, _ string) StrictHandlerFunc {
	return func(ctx context.Context, w http.ResponseWriter, r *http.Request, request any) (any, error) {
		return f(context.WithValue(ctx, queryKey{}, r.URL.Query()), w, r, request)
	}
}

// invocationQuery is the query as keyword arguments, without the API's own
// parameters.
func invocationQuery(ctx context.Context) url.Values {
	query, _ := ctx.Value(queryKey{}).(url.Values)
	out := url.Values{}
	for key, values := range query {
		if key != "wait_seconds" {
			out[key] = values
		}
	}
	return out
}

func (s *Server) describe(ctx context.Context, workspace, app string, kind control.WorkloadKind, name string, version *int) (apitypes.HttpWorkload, error) {
	ws, err := s.workspace(ctx, workspace)
	if err != nil {
		return apitypes.HttpWorkload{}, err
	}
	return s.owners.Edge.Describe(ctx, ws.ID, app, kind, name, version)
}

// GetEndpoint returns an endpoint and its URLs.
func (s *Server) GetEndpoint(ctx context.Context, req GetEndpointRequestObject) (GetEndpointResponseObject, error) {
	out, err := s.describe(ctx, req.Workspace, req.App, control.KindEndpoint, req.Endpoint, req.Params.Version)
	if err != nil {
		return nil, err
	}
	return GetEndpoint200JSONResponse(out), nil
}

// GetAsgi returns an ASGI or realtime app and its URLs.
func (s *Server) GetAsgi(ctx context.Context, req GetAsgiRequestObject) (GetAsgiResponseObject, error) {
	out, err := s.describe(ctx, req.Workspace, req.App, control.KindASGI, req.Endpoint, req.Params.Version)
	if err != nil {
		return nil, err
	}
	return GetAsgi200JSONResponse(out), nil
}

func invokeWait(wait *int) time.Duration {
	if wait == nil {
		return edge.InvokeWait
	}
	return time.Duration(*wait) * time.Second
}

func (s *Server) invoke(ctx context.Context, ws identity.Workspace, release uuid.UUID, body *apitypes.InvocationBody, wait *int) (apitypes.Invocation, error) {
	var raw []byte
	if body != nil {
		raw = *body
	}
	arguments, err := edge.InvocationArguments(raw, invocationQuery(ctx))
	if err != nil {
		return apitypes.Invocation{}, errors.Join(errInvalidRequest, err)
	}
	return edge.Invoke(ctx, s.owners.Execution, s.owners.Listener, ws.ID, release, arguments, invokeWait(wait))
}

// InvokeFunction runs the function's active release with JSON arguments.
func (s *Server) InvokeFunction(ctx context.Context, req InvokeFunctionRequestObject) (InvokeFunctionResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	fn, err := s.owners.Control.GetFunction(ctx, ws.ID, req.App, req.Function)
	if err != nil {
		return nil, err
	}
	out, err := s.invoke(ctx, ws, fn.ActiveRelease.Id, req.Body, req.Params.WaitSeconds)
	if err != nil {
		return nil, err
	}
	return InvokeFunction200JSONResponse(out), nil
}

// InvokeFunctionVersion runs one version of the function.
func (s *Server) InvokeFunctionVersion(ctx context.Context, req InvokeFunctionVersionRequestObject) (InvokeFunctionVersionResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	release, err := s.owners.Execution.FunctionRelease(ctx, ws.ID, req.App, req.Function, req.Version)
	if err != nil {
		return nil, err
	}
	out, err := s.invoke(ctx, ws, release, req.Body, req.Params.WaitSeconds)
	if err != nil {
		return nil, err
	}
	return InvokeFunctionVersion200JSONResponse(out), nil
}

func (s *Server) user(ctx context.Context) (identity.UserID, error) {
	p, ok := principalFrom(ctx)
	if !ok {
		return identity.UserID{}, identity.ErrUnauthenticated
	}
	return p.User, nil
}

// ListDomains lists the caller's custom domains.
func (s *Server) ListDomains(ctx context.Context, req ListDomainsRequestObject) (ListDomainsResponseObject, error) {
	user, err := s.user(ctx)
	if err != nil {
		return nil, err
	}
	after, limit := "", 100
	if req.Params.After != nil {
		after = *req.Params.After
	}
	if req.Params.Limit != nil {
		limit = *req.Params.Limit
	}
	domains, next, err := s.owners.Edge.ListDomains(ctx, user, after, limit)
	if err != nil {
		return nil, err
	}
	out := ListDomains200JSONResponse{Data: domains}
	if next != "" {
		out.Next = &next
	}
	return out, nil
}

// RegisterDomain registers a custom domain for the caller.
func (s *Server) RegisterDomain(ctx context.Context, req RegisterDomainRequestObject) (RegisterDomainResponseObject, error) {
	user, err := s.user(ctx)
	if err != nil {
		return nil, err
	}
	domain, err := s.owners.Edge.RegisterDomain(ctx, user, req.Body.Hostname)
	if err != nil {
		return nil, err
	}
	return RegisterDomain201JSONResponse(domain), nil
}

// GetDomain reads one custom domain.
func (s *Server) GetDomain(ctx context.Context, req GetDomainRequestObject) (GetDomainResponseObject, error) {
	user, err := s.user(ctx)
	if err != nil {
		return nil, err
	}
	domain, err := s.owners.Edge.GetDomain(ctx, user, req.Hostname)
	if err != nil {
		return nil, err
	}
	return GetDomain200JSONResponse(domain), nil
}

// RemoveDomain retires a custom domain.
func (s *Server) RemoveDomain(ctx context.Context, req RemoveDomainRequestObject) (RemoveDomainResponseObject, error) {
	user, err := s.user(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Edge.RemoveDomain(ctx, user, req.Hostname); err != nil {
		return nil, err
	}
	return RemoveDomain204Response{}, nil
}

// endpointError maps the edge's errors; ok is false for errors it does not
// know.
func endpointError(w http.ResponseWriter, err error) bool {
	var (
		invalid  *edge.InvalidDomainError
		conflict *edge.DomainConflictError
		provider *edge.ProviderError
	)
	switch {
	case errors.Is(err, edge.ErrWorkloadNotFound), errors.Is(err, edge.ErrDomainNotFound):
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, err.Error())
	case errors.As(err, &invalid):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, invalid.Error())
	case errors.As(err, &conflict):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, conflict.Error())
	case errors.Is(err, edge.ErrDomainsUnavailable):
		writeJSONError(w, http.StatusServiceUnavailable, apitypes.Unavailable, err.Error())
	case errors.As(err, &provider) && provider.Rejected:
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, provider.Error())
	case errors.As(err, &provider):
		writeJSONError(w, http.StatusServiceUnavailable, apitypes.Unavailable, provider.Error())
	case errors.Is(err, errPreviewNotRunning):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, err.Error())
	case errors.Is(err, edge.ErrSyncRefused):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, err.Error())
	case errors.Is(err, edge.ErrInvalidArguments):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, err.Error())
	default:
		return false
	}
	return true
}

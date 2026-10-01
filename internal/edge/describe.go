package edge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// ErrWorkloadNotFound means no such deployed HTTP workload or version.
var ErrWorkloadNotFound = errors.New("not found")

// route is the path an endpoint answers on; ASGI apps answer on every path,
// so their URLs carry none.
func route(spec apitypes.FunctionSpec) string {
	if spec.Http != nil && spec.Http.Kind == apitypes.HttpKindEndpoint && spec.Http.Route != nil {
		return *spec.Http.Route
	}
	return ""
}

// DeployedURL is where a deployed HTTP workload answers, following its
// active release.
func (e *Edge) DeployedURL(workspace identity.WorkspaceID, app string, spec apitypes.FunctionSpec) string {
	sub := control.Subdomain(uuid.UUID(workspace), app, spec.Name, control.KindOf(spec))
	return e.urls.Deployment(sub, route(spec))
}

// Describe returns a deployed HTTP workload of kind with the URLs it answers
// on, for its active release or version.
func (e *Edge) Describe(ctx context.Context, workspace identity.WorkspaceID, app string, kind control.WorkloadKind, name string, version *int) (apitypes.HttpWorkload, error) {
	params := DescribeWorkloadParams{WorkspaceID: uuid.UUID(workspace), AppName: app, Kind: string(kind), Name: name}
	if version != nil {
		v := int32(*version) //nolint:gosec // the API bounds versions
		params.Version = &v
	}
	row, err := e.queries.DescribeWorkload(ctx, params)
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.HttpWorkload{}, ErrWorkloadNotFound
	}
	if err != nil {
		return apitypes.HttpWorkload{}, fmt.Errorf("describe workload: %w", err)
	}
	var spec apitypes.FunctionSpec
	if err := json.Unmarshal(row.Spec, &spec); err != nil {
		return apitypes.HttpWorkload{}, fmt.Errorf("decode release spec: %w", err)
	}
	if spec.Http == nil {
		return apitypes.HttpWorkload{}, ErrWorkloadNotFound
	}
	path := route(spec)
	out := apitypes.HttpWorkload{
		Name: row.Name, App: row.AppName, Kind: spec.Http.Kind, State: apitypes.HttpWorkloadState(row.DesiredState),
		Release: apitypes.Release{
			Id: row.ReleaseID, Function: row.Name, Version: int(row.Version), CreatedAt: row.CreatedAt, Spec: spec,
		},
		Url:        e.urls.Deployment(row.Subdomain, path),
		VersionUrl: e.urls.Version(row.Subdomain, int(row.Version), path),
		ReleaseUrl: e.urls.Release(row.ReleaseID, path),
	}
	url := out.Url
	out.Release.Url = &url
	if row.Hostname != nil && row.HostnameReady {
		domain := e.urls.Domain(*row.Hostname, path)
		out.DomainUrl = &domain
	}
	return out, nil
}

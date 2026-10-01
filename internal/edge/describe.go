package edge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"

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

// PreviewURL is where a preview answers: its release's host.
func (e *Edge) PreviewURL(release uuid.UUID, spec apitypes.FunctionSpec) string {
	return e.urls.Release(release, route(spec))
}

// Describe returns a deployed HTTP workload of kind with the URLs it answers
// on, for its active release or version.
func (e *Edge) Describe(ctx context.Context, workspace identity.WorkspaceID, app string, kind apitypes.WorkloadKind, name string, version *int) (apitypes.HttpWorkload, error) {
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
	// Every deployed release has a version; working-tree releases are
	// functions.
	if spec.Http == nil || row.Version == nil {
		return apitypes.HttpWorkload{}, ErrWorkloadNotFound
	}
	n := int(*row.Version)
	path := route(spec)
	out := apitypes.HttpWorkload{
		Name: row.Name, App: row.AppName, Kind: spec.Http.Kind, State: apitypes.HttpWorkloadState(row.DesiredState),
		Release: apitypes.Release{
			Id: row.ReleaseID, Function: row.Name, Version: &n, CreatedAt: row.CreatedAt, Spec: spec,
		},
		Url:        e.urls.Deployment(row.Subdomain, path),
		VersionUrl: e.urls.Version(row.Subdomain, n, path),
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

// InvokePath is where the API host serves a deployed endpoint or ASGI app,
// following its active release or pinned to version; the request's own path
// follows it.
func InvokePath(workspace, app string, kind apitypes.WorkloadKind, name string, version *int) string {
	collection := "endpoints"
	if kind == apitypes.WorkloadKindAsgi {
		collection = "asgi"
	}
	p := "/v1/workspaces/" + url.PathEscape(workspace) + "/apps/" + url.PathEscape(app) + "/" + collection + "/" + url.PathEscape(name)
	if version != nil {
		p += "/versions/" + strconv.Itoa(*version)
	}
	return p + "/invoke"
}

// ServeWorkload serves a request the API host received for a deployed
// endpoint or ASGI app of workspace, whose caller it authenticated for the
// workspace and whose credentials it removed. subpath is the request's path
// under the workload. The request is admitted, recorded and forwarded as
// the workload's own host would.
func (e *Edge) ServeWorkload(w http.ResponseWriter, r *http.Request, workspace identity.WorkspaceID, app string, kind apitypes.WorkloadKind, name string, version *int, subpath string) {
	label := control.Subdomain(uuid.UUID(workspace), app, name, kind)
	if version != nil {
		label += "-v" + strconv.Itoa(*version)
	}
	t, err := e.resolveLabel(r.Context(), label, true)
	if err == nil && (t.workload.workspace != workspace || t.workload.kind != kind) {
		err = errNoRoute
	}
	if err != nil {
		e.fail(w, r, err)
		return
	}
	r.URL.Path, r.URL.RawPath = "/"+subpath, ""
	e.serveTarget(w, r, t, true)
}

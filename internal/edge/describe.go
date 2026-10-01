package edge

import (
	"context"
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
func route(spec apitypes.WorkloadSpec) string {
	if spec.Http != nil && spec.Http.Kind == apitypes.HttpKindEndpoint && spec.Http.Route != nil {
		return *spec.Http.Route
	}
	return ""
}

// DeployedURL is where a deployed HTTP workload answers, following its
// active release.
func (e *Edge) DeployedURL(workspace identity.WorkspaceID, app string, spec apitypes.WorkloadSpec) string {
	sub := control.Subdomain(uuid.UUID(workspace), app, spec.Name, spec.Kind)
	return e.urls.Deployment(sub, route(spec))
}

// PreviewURL is where a preview answers: its release's host.
func (e *Edge) PreviewURL(release uuid.UUID, spec apitypes.WorkloadSpec) string {
	return e.urls.Release(release, route(spec))
}

// HTTPUrls is where the deployed HTTP workload of app answers for release,
// one of its deployed versions: on its hosts, its custom hostname once ready,
// and on the API host of workspace.
func (e *Edge) HTTPUrls(ctx context.Context, workspace, app string, workload uuid.UUID, release apitypes.Release) (apitypes.HttpUrls, error) {
	row, err := e.queries.WorkloadRoute(ctx, workload)
	if errors.Is(err, pgx.ErrNoRows) || release.Version == nil {
		return apitypes.HttpUrls{}, ErrWorkloadNotFound
	}
	if err != nil {
		return apitypes.HttpUrls{}, fmt.Errorf("read workload route: %w", err)
	}
	path, kind := route(release.Spec), release.Spec.Kind
	out := apitypes.HttpUrls{
		Url:               e.urls.Deployment(row.Subdomain, path),
		VersionUrl:        e.urls.Version(row.Subdomain, *release.Version, path),
		ReleaseUrl:        e.urls.Release(release.Id, path),
		InvokePath:        InvokePath(workspace, app, kind, release.Name, nil),
		VersionInvokePath: InvokePath(workspace, app, kind, release.Name, release.Version),
	}
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
	p := "/v1/workspaces/" + url.PathEscape(workspace) + "/apps/" + url.PathEscape(app) + "/workloads/" + string(kind) + "/" + url.PathEscape(name)
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

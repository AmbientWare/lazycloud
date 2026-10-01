package edge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// workload is a routed workload as the edge holds it.
type workload struct {
	id            uuid.UUID
	workspace     identity.WorkspaceID
	workspaceName string
	app, name     string
	kind          apitypes.WorkloadKind
	// accepting is false while the workload is stopped or its app paused.
	accepting bool
	subdomain string
	// hostname is the custom hostname, set only once its registration is
	// ready.
	hostname string
	active   *release
}

// release is an immutable release with the settings the edge applies.
type release struct {
	id       uuid.UUID
	workload uuid.UUID
	// preview is set for a release `lazycloud serve` runs, and workingTree
	// for one a local process prepared; neither is a deployed version.
	preview     bool
	workingTree bool
	spec        apitypes.FunctionSpec
	// capacity is the requests one container admits: workers times
	// concurrency.
	capacity   int
	authorized bool
	timeout    time.Duration
	keepWarm   time.Duration
	maxPending int
	route      string
	methods    []string
	attempts   int
}

func newRelease(id, workload uuid.UUID, version *int32, rawSpec []byte) (*release, error) {
	var spec apitypes.FunctionSpec
	if err := json.Unmarshal(rawSpec, &spec); err != nil {
		return nil, fmt.Errorf("decode release spec: %w", err)
	}
	r := &release{
		id: id, workload: workload, preview: version != nil && *version < 0, workingTree: version == nil, spec: spec,
		capacity: 1, authorized: true, timeout: time.Hour, maxPending: 100, attempts: 1,
	}
	concurrency := 1
	if spec.Concurrency != nil {
		concurrency = *spec.Concurrency
	}
	if spec.TimeoutSeconds != nil {
		r.timeout = time.Duration(*spec.TimeoutSeconds) * time.Second
	}
	if spec.KeepWarmSeconds != nil {
		r.keepWarm = time.Duration(*spec.KeepWarmSeconds) * time.Second
	}
	if spec.MaxPendingTasks != nil {
		r.maxPending = *spec.MaxPendingTasks
	}
	if spec.RetryPolicy != nil {
		r.attempts = max(1, spec.RetryPolicy.MaxAttempts)
	}
	if h := spec.Http; h != nil {
		workers := 1
		if h.Workers != nil {
			workers = *h.Workers
		}
		r.capacity = max(1, workers*concurrency)
		if h.Authorized != nil {
			r.authorized = *h.Authorized
		}
		if h.Route != nil {
			r.route = *h.Route
		}
		if h.Methods != nil {
			for _, m := range *h.Methods {
				r.methods = append(r.methods, string(m))
			}
		}
	}
	return r, nil
}

func (r *release) allows(method string) bool {
	return slices.Contains(r.methods, method)
}

// routeTable is every claimed route, replaced whole on each reload.
type routeTable struct {
	bySubdomain map[string]*workload
	byHostname  map[string]*workload
	byID        map[uuid.UUID]*workload
}

func (e *Edge) loadRoutes(ctx context.Context) (*routeTable, error) {
	rows, err := e.queries.Routes(ctx)
	if err != nil {
		return nil, fmt.Errorf("load routes: %w", err)
	}
	t := &routeTable{
		bySubdomain: make(map[string]*workload, len(rows)),
		byHostname:  map[string]*workload{},
		byID:        make(map[uuid.UUID]*workload, len(rows)),
	}
	for _, row := range rows {
		w := &workload{
			id: row.WorkloadID, workspace: identity.WorkspaceID(row.WorkspaceID), workspaceName: row.WorkspaceName,
			app: row.AppName, name: row.Name, kind: apitypes.WorkloadKind(row.Kind),
			accepting: row.DesiredState == "active" && row.AppState == "active",
			subdomain: row.Subdomain,
		}
		if row.Hostname != nil && row.HostnameReady {
			w.hostname = *row.Hostname
			t.byHostname[w.hostname] = w
		}
		if row.ReleaseID != nil {
			w.active, err = newRelease(*row.ReleaseID, w.id, row.Version, row.Spec)
			if err != nil {
				return nil, err
			}
		}
		t.bySubdomain[w.subdomain] = w
		t.byID[w.id] = w
	}
	return t, nil
}

// errNoRoute means the host names no workload, release or container.
var errNoRoute = errors.New("no workload answers on this host")

// target is what one request is for.
type target struct {
	workload *workload
	release  *release
	// latest lets the request fall back to an older ready release while the
	// active one has no ready container.
	latest bool
	// container restricts the request to one container.
	container *uuid.UUID
}

// resolve finds what a request host names: a custom hostname, a deployment
// label (latest or a pinned version), a release id or a container id.
func (e *Edge) resolve(ctx context.Context, host string) (target, error) {
	arrived := time.Now()
	label, under := e.urls.hostLabel(host)
	if under {
		if id, err := uuid.Parse(label); err == nil {
			return e.resolveID(ctx, id)
		}
	}
	subdomain, version := deploymentLabel(label)
	lookup := func(routes *routeTable) *workload {
		if !under {
			return routes.byHostname[label]
		}
		return routes.bySubdomain[subdomain]
	}
	w := lookup(e.routes.Load())
	if w == nil || w.active == nil {
		// A deploy that committed moments ago may not have reached the table
		// yet; misses reload it at most every missReload.
		if err := e.reloadOnMiss(ctx, arrived); err != nil {
			return target{}, err
		}
		w = lookup(e.routes.Load())
	}
	if w == nil {
		return target{}, errNoRoute
	}
	if !under {
		if w.active == nil {
			return target{}, errNoRoute
		}
		return target{workload: w, release: w.active, latest: true}, nil
	}
	if version == 0 {
		if w.active == nil {
			return target{}, errNoRoute
		}
		return target{workload: w, release: w.active, latest: true}, nil
	}
	key := versionKey{workload: w.id, version: version}
	e.mu.Lock()
	id, known := e.versions[key]
	e.mu.Unlock()
	if !known {
		id, err := e.queries.ReleaseOfVersion(ctx, ReleaseOfVersionParams{WorkloadID: w.id, Version: new(int32(version))}) //nolint:gosec // parsed from a label
		if errors.Is(err, pgx.ErrNoRows) {
			return target{}, errNoRoute
		}
		if err != nil {
			return target{}, fmt.Errorf("read version: %w", err)
		}
		// A version names one release forever.
		e.mu.Lock()
		if len(e.versions) >= maxCachedReleases {
			clear(e.versions)
		}
		e.versions[key] = id
		e.mu.Unlock()
		return e.versionTarget(ctx, w, id)
	}
	return e.versionTarget(ctx, w, id)
}

func (e *Edge) versionTarget(ctx context.Context, w *workload, id uuid.UUID) (target, error) {
	r, err := e.release(ctx, id)
	if err != nil {
		return target{}, err
	}
	return target{workload: w, release: r}, nil
}

// resolveID resolves a release id, including a preview's, or a container
// id.
func (e *Edge) resolveID(ctx context.Context, id uuid.UUID) (target, error) {
	row, err := e.queries.ReleaseRoute(ctx, id)
	var container *uuid.UUID
	if errors.Is(err, pgx.ErrNoRows) {
		release, cerr := e.queries.ContainerRelease(ctx, id)
		if errors.Is(cerr, pgx.ErrNoRows) {
			return target{}, errNoRoute
		}
		if cerr != nil {
			return target{}, fmt.Errorf("read container: %w", cerr)
		}
		container = &id
		row, err = e.queries.ReleaseRoute(ctx, release)
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return target{}, errNoRoute
	}
	if err != nil {
		return target{}, fmt.Errorf("read release: %w", err)
	}
	r, err := e.release(ctx, row.ReleaseID)
	if err != nil {
		return target{}, err
	}
	w := &workload{
		id: row.WorkloadID, workspace: identity.WorkspaceID(row.WorkspaceID), workspaceName: row.WorkspaceName,
		app: row.AppName, name: row.Name, kind: apitypes.WorkloadKind(row.Kind),
		accepting: row.DesiredState == "active" && row.AppState == "active",
	}
	switch {
	case r.preview:
		// A preview runs while its lease lives, whatever its workload's state.
		w.accepting = row.PreviewLive
	case r.workingTree:
		w.accepting = row.AppState == "active"
	}
	return target{workload: w, release: r, container: container}, nil
}

// release returns a release, cached: releases never change.
func (e *Edge) release(ctx context.Context, id uuid.UUID) (*release, error) {
	e.mu.Lock()
	r := e.releases[id]
	e.mu.Unlock()
	if r != nil {
		return r, nil
	}
	row, err := e.queries.ReleaseRoute(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNoRoute
	}
	if err != nil {
		return nil, fmt.Errorf("read release: %w", err)
	}
	r, err = newRelease(row.ReleaseID, row.WorkloadID, row.Version, row.Spec)
	if err != nil {
		return nil, err
	}
	e.mu.Lock()
	if len(e.releases) >= maxCachedReleases {
		clear(e.releases)
	}
	e.releases[id] = r
	e.mu.Unlock()
	return r, nil
}

// maxCachedReleases bounds the release cache; it starts over when full.
const maxCachedReleases = 10_000

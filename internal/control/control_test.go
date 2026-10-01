package control

import (
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

var testSource = strings.Repeat("ab", 32)

func fixture(t *testing.T) (*pgxpool.Pool, identity.WorkspaceID) {
	t.Helper()
	pool := dbtest.New(t)
	var ws uuid.UUID
	err := pool.QueryRow(t.Context(), `
with ws as (insert into workspaces (name) values ('ws') returning id),
     src as (insert into source_objects (workspace_id, sha256, size_bytes) select id, decode($1, 'hex'), 10 from ws)
select id from ws`, testSource).Scan(&ws)
	if err != nil {
		t.Fatal(err)
	}
	dbtest.OwnWorkspaces(t, pool)
	return pool, identity.WorkspaceID(ws)
}

func function(name string) apitypes.FunctionSpec {
	return apitypes.FunctionSpec{
		Name:      name,
		Handler:   "app:" + name,
		Source:    apitypes.SourceRef{Sha256: testSource},
		Image:     apitypes.ImageSpec{PythonVersion: apitypes.N312},
		Resources: apitypes.Resources{CpuMillis: 1000, MemoryMib: 512},
	}
}

func deploy(t *testing.T, c *Control, ws identity.WorkspaceID, prune bool, specs ...apitypes.FunctionSpec) apitypes.Deployment {
	t.Helper()
	d, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Functions: specs, Prune: &prune})
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestDeployVersionsOnlyChangedSpecs(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)

	first := deploy(t, c, ws, false, function("summarize"), function("export"))
	if *first.Releases[0].Version != 1 || *first.Releases[1].Version != 1 {
		t.Fatalf("first deploy versions %d, %d; want 1, 1", *first.Releases[0].Version, *first.Releases[1].Version)
	}
	if spec := first.Releases[0].Spec; *spec.TimeoutSeconds != 3600 || spec.RetryPolicy.MaxAttempts != 1 || *spec.Autoscaler.MaxContainers != 1 {
		t.Fatalf("defaults not resolved: %+v", spec)
	}

	// Stating a default explicitly is the same spec.
	explicit := function("summarize")
	timeout := 3600
	explicit.TimeoutSeconds = &timeout
	same := deploy(t, c, ws, false, explicit, function("export"))
	if same.Releases[0].Id != first.Releases[0].Id || same.Releases[1].Id != first.Releases[1].Id {
		t.Fatal("identical specs created new releases")
	}

	changed := function("summarize")
	changed.Concurrency = new(4)
	next := deploy(t, c, ws, false, changed, function("export"))
	if *next.Releases[0].Version != 2 || next.Releases[1].Id != first.Releases[1].Id {
		t.Fatalf("changed deploy: summarize v%d, export reused %v; want v2 and reuse",
			*next.Releases[0].Version, next.Releases[1].Id == first.Releases[1].Id)
	}
	got, err := c.GetFunction(t.Context(), ws, "reports", "summarize")
	if err != nil {
		t.Fatal(err)
	}
	if got.ActiveRelease.Id != next.Releases[0].Id || *got.ActiveRelease.Spec.Concurrency != 4 {
		t.Fatalf("active release %v, want %v", got.ActiveRelease.Id, next.Releases[0].Id)
	}

	pruned := deploy(t, c, ws, true, changed)
	if len(pruned.Pruned) != 1 || pruned.Pruned[0] != "export" || pruned.RemovedVersions != 1 {
		t.Fatalf("pruned %v removing %d versions, want [export] removing 1", pruned.Pruned, pruned.RemovedVersions)
	}
	if _, err := c.GetFunction(t.Context(), ws, "reports", "export"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("pruned function read error %v, want not found", err)
	}
	// A pruned function is deleted, so deploying it again starts over.
	again := deploy(t, c, ws, false, function("export"))
	if again.Releases[0].Id == first.Releases[1].Id || *again.Releases[0].Version != 1 {
		t.Fatalf("redeploying a pruned function reused %v at v%d", again.Releases[0].Id, *again.Releases[0].Version)
	}
	if export, _ := c.GetFunction(t.Context(), ws, "reports", "export"); export.State != apitypes.FunctionStateActive {
		t.Fatalf("redeployed function state %v, want active", export.State)
	}
}

func TestConcurrentDeploysKeepVersionsDistinct(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	const deploys = 8
	var wg sync.WaitGroup
	errs := make(chan error, deploys)
	for n := range deploys {
		wg.Go(func() {
			spec := function("summarize")
			spec.KeepWarmSeconds = new(n)
			_, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{spec}})
			errs <- err
		})
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	var releases, maxVersion int
	if err := pool.QueryRow(t.Context(), "select count(*), max(version) from releases").Scan(&releases, &maxVersion); err != nil {
		t.Fatal(err)
	}
	if releases != deploys || maxVersion != deploys {
		t.Fatalf("%d releases up to v%d, want %d up to v%d", releases, maxVersion, deploys, deploys)
	}
}

func TestDeployRequiresRegisteredSourceAndChangesNothing(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	missing := function("export")
	missing.Source.Sha256 = strings.Repeat("cd", 32)
	_, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{
		Functions: []apitypes.FunctionSpec{function("summarize"), missing},
	})
	var sourceErr *SourceMissingError
	if !errors.As(err, &sourceErr) || sourceErr.Function != "export" {
		t.Fatalf("got %v, want SourceMissingError for export", err)
	}
	var apps int
	if err := pool.QueryRow(t.Context(), "select count(*) from apps").Scan(&apps); err != nil {
		t.Fatal(err)
	}
	if apps != 0 {
		t.Fatalf("a failed deploy left %d apps", apps)
	}
}

package control

import (
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
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

func function(name string) apitypes.WorkloadSpec {
	return apitypes.WorkloadSpec{
		Kind:      apitypes.WorkloadKindFunction,
		Name:      name,
		Handler:   new("app:" + name),
		Source:    apitypes.SourceRef{Sha256: testSource},
		Image:     apitypes.ImageSpec{PythonVersion: apitypes.N312},
		Resources: apitypes.Resources{CpuMillis: 1000, MemoryMib: 512},
	}
}

// functionRelease reads the release the live function name of app reports runs.
func functionRelease(t *testing.T, c *Control, ws identity.WorkspaceID, name string) (apitypes.Release, error) {
	t.Helper()
	id, err := c.FindWorkload(t.Context(), ws, WorkloadRef{App: "reports", Kind: apitypes.WorkloadKindFunction, Name: name})
	if err != nil {
		return apitypes.Release{}, err
	}
	return c.Release(t.Context(), ws, id, nil)
}

func deploy(t *testing.T, c *Control, ws identity.WorkspaceID, prune bool, specs ...apitypes.WorkloadSpec) apitypes.Deployment {
	t.Helper()
	d, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Workloads: specs, Prune: &prune})
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
	got, err := functionRelease(t, c, ws, "summarize")
	if err != nil {
		t.Fatal(err)
	}
	if got.Id != next.Releases[0].Id || *got.Spec.Concurrency != 4 {
		t.Fatalf("active release %v, want %v", got.Id, next.Releases[0].Id)
	}

	pruned := deploy(t, c, ws, true, changed)
	if len(pruned.Pruned) != 1 || pruned.Pruned[0] != "export" || pruned.RemovedVersions != 1 {
		t.Fatalf("pruned %v removing %d versions, want [export] removing 1", pruned.Pruned, pruned.RemovedVersions)
	}
	if _, err := functionRelease(t, c, ws, "export"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("pruned function read error %v, want not found", err)
	}
	// A pruned function is deleted, so deploying it again starts over.
	again := deploy(t, c, ws, false, function("export"))
	if again.Releases[0].Id == first.Releases[1].Id || *again.Releases[0].Version != 1 {
		t.Fatalf("redeploying a pruned function reused %v at v%d", again.Releases[0].Id, *again.Releases[0].Version)
	}
	if _, err := functionRelease(t, c, ws, "export"); err != nil {
		t.Fatalf("redeployed function: %v", err)
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
			_, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Workloads: []apitypes.WorkloadSpec{spec}})
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
		Workloads: []apitypes.WorkloadSpec{function("summarize"), missing},
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

// A workload only the platform fleet can serve is refused when it names
// only models the fleet does not offer; one pinned to a joined machine, or
// in a connected account's workspace, deploys any model.
func TestDeployRefusesGPUModelsOnlyWhereTheFleetMustServeThem(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	h100 := function("train")
	h100.Resources.Gpu = &[]apitypes.GpuType{apitypes.H100}
	deployOne := func(spec apitypes.WorkloadSpec) error {
		_, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Workloads: []apitypes.WorkloadSpec{spec}})
		return err
	}
	var unoffered *billing.GPUUnavailableError
	if err := deployOne(h100); !errors.As(err, &unoffered) || !strings.Contains(err.Error(), "coming soon") {
		t.Fatalf("H100 on the fleet: %v", err)
	}
	fallback := h100
	fallback.Resources.Gpu = &[]apitypes.GpuType{apitypes.H100, apitypes.L4}
	if err := deployOne(fallback); err != nil {
		t.Fatalf("H100 then L4 on the fleet: %v", err)
	}
	pinned := h100
	pinned.Placement = &apitypes.Placement{Machine: new("gpu-1")}
	if err := deployOne(pinned); err != nil {
		t.Fatalf("H100 on a joined machine: %v", err)
	}
	if _, err := pool.Exec(t.Context(), `
with owner as (select user_id from workspace_members where workspace_id = $1 and role = 'owner'),
     conn as (insert into cloud_connections (account_id, aws_account_id, phase) select user_id, '123456789012', 'ready' from owner returning id)
update workspaces set connection_id = (select id from conn) where id = $1`, uuid.UUID(ws)); err != nil {
		t.Fatal(err)
	}
	if err := deployOne(h100); err != nil {
		t.Fatalf("H100 in a connected account: %v", err)
	}
}

package control

import (
	"errors"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

func withConcurrency(spec apitypes.FunctionSpec, n int) apitypes.FunctionSpec {
	spec.Concurrency = &n
	return spec
}

func TestAppPauseResumeAndDeleteFreeTheName(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	first := deploy(t, c, ws, false, function("summarize"))
	if _, err := c.Deploy(t.Context(), ws, "billing", apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{function("charge")}}); err != nil {
		t.Fatal(err)
	}
	// Preparing a working-tree release creates an app that is not listed.
	if _, err := c.PrepareRelease(t.Context(), ws, "scratch", "probe", function("probe")); err != nil {
		t.Fatal(err)
	}

	page, err := c.ListApps(t.Context(), ws, AppFilter{}, 1, "")
	if err != nil || len(page.Apps) != 1 || page.Apps[0].Name != "billing" || page.Next == "" {
		t.Fatalf("first page %+v %v", page, err)
	}
	page, err = c.ListApps(t.Context(), ws, AppFilter{}, 1, page.Next)
	if err != nil || len(page.Apps) != 1 || page.Apps[0].Name != "reports" || page.Next != "" || page.Apps[0].Workloads != 1 {
		t.Fatalf("second page %+v %v", page, err)
	}

	paused, err := c.PauseApp(t.Context(), ws, "reports")
	if err != nil || paused.State != apitypes.AppStatePaused {
		t.Fatalf("pause %+v %v", paused, err)
	}
	state := AppPaused
	if page, err := c.ListApps(t.Context(), ws, AppFilter{State: &state}, 10, ""); err != nil || len(page.Apps) != 1 || page.Apps[0].Name != "reports" {
		t.Fatalf("paused apps %+v %v", page, err)
	}
	// Search matches part of the name on the server.
	for term, want := range map[string]int{"ILL": 1, "port": 1, "nope": 0} {
		if page, err := c.ListApps(t.Context(), ws, AppFilter{Search: &term}, 10, ""); err != nil || len(page.Apps) != want {
			t.Fatalf("apps matching %q: %+v %v, want %d", term, page.Apps, err, want)
		}
	}
	if resumed, err := c.ResumeApp(t.Context(), ws, first.App.Id.String()); err != nil || resumed.State != apitypes.AppStateActive {
		t.Fatalf("resume by id %+v %v", resumed, err)
	}

	deleted, err := c.DeleteApp(t.Context(), ws, "reports")
	if err != nil || deleted.State != apitypes.AppStateDeleted {
		t.Fatalf("delete %+v %v", deleted, err)
	}
	if _, err := c.GetApp(t.Context(), ws, "reports"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted app by name %v", err)
	}
	if app, err := c.GetApp(t.Context(), ws, first.App.Id.String()); err != nil || app.State != apitypes.AppStateDeleted {
		t.Fatalf("deleted app by id %+v %v", app, err)
	}
	if _, err := c.PauseApp(t.Context(), ws, first.App.Id.String()); !errors.Is(err, ErrNotFound) {
		t.Fatalf("pause of a deleted app %v", err)
	}
	again := deploy(t, c, ws, false, function("summarize"))
	if again.App.Id == first.App.Id || *again.Releases[0].Version != 1 {
		t.Fatalf("redeploy after delete reused app %v at v%d", again.App.Id, *again.Releases[0].Version)
	}
	if _, err := c.GetFunction(t.Context(), ws, "reports", "summarize"); err != nil {
		t.Fatalf("function of the new app %v", err)
	}
}

func TestDeploymentStopStartVersionsAndDelete(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	v1 := deploy(t, c, ws, false, function("summarize"))
	deploy(t, c, ws, false, withConcurrency(function("summarize"), 2))
	name := "summarize"
	list, err := c.ListDeployments(t.Context(), ws, DeploymentFilter{Name: &name}, 10, "")
	if err != nil || len(list.Deployments) != 1 || *list.Deployments[0].Version != 2 {
		t.Fatalf("deployments %+v %v", list, err)
	}
	// Search matches part of the workload or the app name.
	for _, term := range []string{"MARIZ", "report"} {
		if found, err := c.ListDeployments(t.Context(), ws, DeploymentFilter{Search: &term}, 10, ""); err != nil || len(found.Deployments) != 1 {
			t.Fatalf("deployments matching %q: %+v %v", term, found, err)
		}
	}
	if found, _ := c.ListDeployments(t.Context(), ws, DeploymentFilter{Search: &name, App: &name}, 10, ""); len(found.Deployments) != 0 {
		t.Fatalf("search within another app %+v", found)
	}
	id := WorkloadID(list.Deployments[0].Id)

	stopped, err := c.StopDeployment(t.Context(), ws, id)
	if err != nil || stopped.State != apitypes.WorkloadStateStopped {
		t.Fatalf("stop %+v %v", stopped, err)
	}
	one := 1
	started, err := c.StartDeployment(t.Context(), ws, id, &one)
	if err != nil || started.State != apitypes.WorkloadStateActive || *started.ReleaseId != v1.Releases[0].Id {
		t.Fatalf("start on v1 %+v %v", started, err)
	}
	nine := 9
	if _, err := c.StartDeployment(t.Context(), ws, id, &nine); !errors.Is(err, ErrVersionNotFound) {
		t.Fatalf("start on a missing version %v", err)
	}

	versions, err := c.ListVersions(t.Context(), ws, id, 1, "")
	if err != nil || len(versions.Versions) != 1 || versions.Versions[0].Version != 2 || versions.Versions[0].Active || versions.Next != "2" {
		t.Fatalf("first version page %+v %v", versions, err)
	}
	versions, err = c.ListVersions(t.Context(), ws, id, 1, versions.Next)
	if err != nil || len(versions.Versions) != 1 || versions.Versions[0].ReleaseId != v1.Releases[0].Id || !versions.Versions[0].Active {
		t.Fatalf("second version page %+v %v", versions, err)
	}
	if _, err := c.ListVersions(t.Context(), ws, id, 1, "x"); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("bad version cursor %v", err)
	}

	deleted, err := c.DeleteDeployment(t.Context(), ws, id)
	if err != nil || deleted.State != apitypes.WorkloadStateDeleted {
		t.Fatalf("delete %+v %v", deleted, err)
	}
	if list, err := c.ListDeployments(t.Context(), ws, DeploymentFilter{}, 10, ""); err != nil || len(list.Deployments) != 0 {
		t.Fatalf("deployments after delete %+v %v", list, err)
	}
	if _, err := c.StopDeployment(t.Context(), ws, id); !errors.Is(err, ErrNotFound) {
		t.Fatalf("stop of a deleted deployment %v", err)
	}
	if _, err := c.GetDeployment(t.Context(), ws, WorkloadID(uuid.New())); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown deployment %v", err)
	}
}

func TestPlanDeploymentActions(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	deploy(t, c, ws, false, function("keep"), function("drop"))
	deploy(t, c, ws, false, withConcurrency(function("keep"), 3))
	req := apitypes.DeploymentPlanRequest{Workloads: []apitypes.WorkloadIdentity{
		{Kind: apitypes.WorkloadKindFunction, Name: "new"}, {Kind: apitypes.WorkloadKindFunction, Name: "keep"},
	}}
	plan, err := c.PlanDeployment(t.Context(), ws, "reports", req)
	if err != nil {
		t.Fatal(err)
	}
	want := []apitypes.DeploymentPlanItem{
		{Kind: "function", Name: "keep", Action: apitypes.Redeploy, Versions: 2},
		{Kind: "function", Name: "new", Action: apitypes.Add},
		{Kind: "function", Name: "drop", Action: apitypes.Retain, Versions: 1},
	}
	if len(plan.Items) != len(want) {
		t.Fatalf("plan %+v", plan.Items)
	}
	for n, item := range want {
		if plan.Items[n] != item {
			t.Fatalf("plan item %d %+v, want %+v", n, plan.Items[n], item)
		}
	}
	prune := true
	req.Prune = &prune
	if plan, err := c.PlanDeployment(t.Context(), ws, "reports", req); err != nil || plan.Items[2].Action != apitypes.Remove {
		t.Fatalf("pruning plan %+v %v", plan, err)
	}
	if plan, err := c.PlanDeployment(t.Context(), ws, "fresh", req); err != nil || len(plan.Items) != 2 || plan.Items[0].Action != apitypes.Add {
		t.Fatalf("plan for a new app %+v %v", plan, err)
	}
}

func TestPrepareReleaseReusesMatchingDefinitions(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	deployed := deploy(t, c, ws, false, function("summarize"))

	same, err := c.PrepareRelease(t.Context(), ws, "reports", "summarize", function("summarize"))
	if err != nil || same.Id != deployed.Releases[0].Id || *same.Version != 1 {
		t.Fatalf("unchanged definition %+v %v, want the active release", same, err)
	}
	changed, err := c.PrepareRelease(t.Context(), ws, "reports", "summarize", withConcurrency(function("summarize"), 2))
	if err != nil || changed.Id == same.Id || changed.Version != nil {
		t.Fatalf("changed definition %+v %v, want a new unversioned release", changed, err)
	}
	again, err := c.PrepareRelease(t.Context(), ws, "reports", "summarize", withConcurrency(function("summarize"), 2))
	if err != nil || again.Id != changed.Id {
		t.Fatalf("repeated definition %+v %v, want %v", again, err, changed.Id)
	}
	fn, err := c.GetFunction(t.Context(), ws, "reports", "summarize")
	if err != nil || fn.ActiveRelease.Id != deployed.Releases[0].Id {
		t.Fatalf("active release after prepare %+v %v", fn, err)
	}
	// A later deploy of the prepared definition gets the next version.
	next := deploy(t, c, ws, false, withConcurrency(function("summarize"), 2))
	if *next.Releases[0].Version != 2 {
		t.Fatalf("deploy after prepare at v%d", *next.Releases[0].Version)
	}
	if _, err := c.PrepareRelease(t.Context(), ws, "reports", "other", function("summarize")); err == nil {
		t.Fatal("prepare accepted a spec named for another function")
	}
}

func TestStoppedWorkloadRunsUnchangedWorkingTreeCalls(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	deployed := deploy(t, c, ws, false, function("summarize"))
	list, err := c.ListDeployments(t.Context(), ws, DeploymentFilter{}, 10, "")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.StopDeployment(t.Context(), ws, WorkloadID(list.Deployments[0].Id)); err != nil {
		t.Fatal(err)
	}
	release, err := c.PrepareRelease(t.Context(), ws, "reports", "summarize", function("summarize"))
	if err != nil || release.Id == deployed.Releases[0].Id || release.Version != nil {
		t.Fatalf("prepare on a stopped workload %+v %v, want a new unversioned release", release, err)
	}
	tasks, err := execution.NewExecution(pool).Submit(t.Context(), execution.SubmitRequest{
		Workspace: ws, App: "reports", Function: "summarize", Release: &release.Id,
		Inputs: []execution.TaskInput{{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": []}`)}}},
	})
	if err != nil || len(tasks) != 1 {
		t.Fatalf("submit to the prepared release %v %v", tasks, err)
	}
	if again, err := c.PrepareRelease(t.Context(), ws, "reports", "summarize", function("summarize")); err != nil || again.Id != release.Id {
		t.Fatalf("second prepare %+v %v, want %v", again, err, release.Id)
	}
}

func TestPruneWithoutFunctionsDeletesTheDeployedOnes(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	deploy(t, c, ws, false, function("a"), function("b"))
	if _, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{}); !errors.Is(err, ErrNothingToDeploy) {
		t.Fatalf("empty deploy without prune %v", err)
	}
	pruned := deploy(t, c, ws, true)
	if len(pruned.Pruned) != 2 || pruned.RemovedVersions != 2 || pruned.App.Workloads != 0 {
		t.Fatalf("empty pruning deploy %+v", pruned)
	}
}

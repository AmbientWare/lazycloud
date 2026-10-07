package acceptance

import (
	"bytes"
	"encoding/json"
	"net/http"
	"slices"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// A deploy its account's plan or workspace cannot run is refused through
// the public API with every reason and before anything changes, and the
// plan reports the same reasons before anything builds.
func TestDeployPastThePlanIsRefusedBeforeAnythingChanges(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": endpointApp})
	if _, err := p.pool.Exec(t.Context(), `update billing_accounts set complimentary_since = null`); err != nil {
		t.Fatal(err)
	}
	box := spec("box", "", source, nil)
	box.Kind, box.Handler = apitypes.WorkloadKindPod, nil
	box.Pod = &apitypes.PodSpec{Kind: apitypes.PodKindDevbox}
	box.Disks = &[]apitypes.DiskMountSpec{{Name: "box", SizeBytes: 10 << 30, MountPath: "/"}}
	pinned := spec("count_words", "app:count_words", source, nil)
	pinned.Placement = &apitypes.Placement{Region: new(apitypes.Region("us-east-2"))}
	pinned.Secrets = &[]string{"OPENAI_API_KEY"}
	want := []string{
		"function count_words region_selection",
		"function count_words missing_secrets",
		"pod box disk_allowance",
	}

	body, err := json.Marshal(apitypes.DeploymentRequest{Workloads: []apitypes.WorkloadSpec{box, pinned}})
	if err != nil {
		t.Fatal(err)
	}
	resp, err := p.request(http.MethodPost, p.api+"/v1/workspaces/ws/apps/demo/deployments", p.token, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	var refused apitypes.Error
	if err := json.NewDecoder(resp.Body).Decode(&refused); err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusPaymentRequired || refused.Code != apitypes.PaymentRequired || refused.Refusals == nil {
		t.Fatalf("refused deploy answered %d %+v", resp.StatusCode, refused)
	}
	if got := gates(*refused.Refusals); !slices.Equal(got, want) {
		t.Fatalf("deploy refusals %v, want %v", got, want)
	}
	var apps int
	if err := p.pool.QueryRow(t.Context(), "select count(*) from apps").Scan(&apps); err != nil || apps != 0 {
		t.Fatalf("a refused deploy left %d apps, %v", apps, err)
	}

	var plan apitypes.DeploymentPlan
	request := apitypes.DeploymentPlanRequest{Workloads: []apitypes.DeploymentPlanWorkload{
		{Kind: box.Kind, Name: box.Name, Resources: &box.Resources, Disks: box.Disks},
		{Kind: pinned.Kind, Name: pinned.Name, Resources: &pinned.Resources, Placement: pinned.Placement, Secrets: pinned.Secrets},
	}}
	if status := p.apiCall(http.MethodPost, "/v1/workspaces/ws/apps/demo/deployment-plan", request, &plan); status != http.StatusOK {
		t.Fatalf("plan: %d", status)
	}
	var planned []apitypes.DeploymentRefusal
	for _, item := range plan.Items {
		if item.Refusals != nil {
			planned = append(planned, *item.Refusals...)
		}
	}
	if got := gates(planned); !slices.Equal(got, want) {
		t.Fatalf("plan refusals %v, want %v", got, want)
	}
}

func gates(refusals []apitypes.DeploymentRefusal) []string {
	out := make([]string, len(refusals))
	for n, r := range refusals {
		out[n] = string(r.Kind) + " " + r.Name + " " + string(r.Gate)
	}
	return out
}

package control

import (
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// account puts the workspace owner on terms, with or without a saved card,
// and not complimentary.
func account(t *testing.T, pool *pgxpool.Pool, ws identity.WorkspaceID, terms string, card bool) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), `
update billing_accounts set complimentary_since = null, terms_version = $2,
       payment_method_attached_at = case when $3 then now() end
where user_id = (select user_id from workspace_members where workspace_id = $1 and role = 'owner')`,
		uuid.UUID(ws), terms, card); err != nil {
		t.Fatal(err)
	}
}

func devbox(name string, sizeBytes int64) apitypes.WorkloadSpec {
	spec := pod(name, apitypes.PodKindDevbox)
	spec.Pod.Ports = nil
	spec.Disks = &[]apitypes.DiskMountSpec{{Name: name, SizeBytes: sizeBytes, MountPath: "/"}}
	return spec
}

// refusedBy is the gates and remedies a deploy of specs is refused with, by
// workload name; nil when it deploys.
func refusedBy(t *testing.T, c *Control, ws identity.WorkspaceID, specs ...apitypes.WorkloadSpec) []string {
	t.Helper()
	_, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Workloads: specs})
	if err == nil {
		return nil
	}
	var refused *RefusedError
	if !errors.As(err, &refused) {
		t.Fatalf("deploy: %v", err)
	}
	var out []string
	for _, r := range refused.Refusals {
		out = append(out, r.Name+" "+string(r.Gate)+": "+r.Remedy)
	}
	return out
}

func TestDeployRefusesDisksPastThePlansAllowance(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	account(t, pool, ws, "free-v2", true)
	_, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Workloads: []apitypes.WorkloadSpec{devbox("box", 10<<30)}})
	var refused *RefusedError
	if !errors.As(err, &refused) || len(refused.Refusals) != 1 {
		t.Fatalf("a disk on Free: %v", err)
	}
	if r := refused.Refusals[0]; r.Gate != apitypes.DiskAllowance || r.Remedy != "upgrade to Team" ||
		r.Message != "this workspace's disks would declare 10 GiB, more than its plan allows (0 GiB)" {
		t.Fatalf("refusal %+v", r)
	}
	var apps int
	if err := pool.QueryRow(t.Context(), "select count(*) from apps").Scan(&apps); err != nil || apps != 0 {
		t.Fatalf("a refused deploy left %d apps, %v", apps, err)
	}
}

// A paid or complimentary account deploys within its allowance, and a
// redeploy counts each disk once against the disk it already has.
func TestDeployCountsEachDiskOnce(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	box := devbox("box", 600<<30)
	if got := refusedBy(t, c, ws, box); got != nil {
		t.Fatalf("complimentary: %v", got)
	}
	account(t, pool, ws, "team-v3", true)
	if _, err := pool.Exec(t.Context(), "insert into disks (workspace_id, name, size_bytes) values ($1, 'box', $2)", uuid.UUID(ws), int64(600<<30)); err != nil {
		t.Fatal(err)
	}
	box.Environment = &map[string]string{"CHANGED": "1"}
	if got := refusedBy(t, c, ws, box); got != nil {
		t.Fatalf("redeploy of a 600 GiB disk on Team's 1024: %v", got)
	}
	second := devbox("other", 600<<30)
	if got := refusedBy(t, c, ws, box, second); !slices.Equal(got, []string{"other disk_allowance: delete disks it no longer needs or declare smaller ones"}) {
		t.Fatalf("a second 600 GiB disk: %v", got)
	}
}

// Every plan rule a start would refuse is refused at deploy, all at once
// and before anything changes; the same specs deploy on a complimentary
// account.
func TestDeployRefusesWhatThePlanCannotRun(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	pinned := function("pinned")
	pinned.Placement = &apitypes.Placement{Region: new(apitypes.UsEast)}
	model := function("model")
	model.Resources.Gpu = &[]apitypes.GpuType{apitypes.L40S}
	cards := function("cards")
	cards.Resources.Gpu, cards.Resources.GpuCount = &[]apitypes.GpuType{apitypes.L4}, new(2)
	warm := function("warm")
	warm.Autoscaler = &apitypes.Autoscaler{MinContainers: new(11), MaxContainers: new(11)}
	warmGPU := function("warmgpu")
	warmGPU.Resources.Gpu = &[]apitypes.GpuType{apitypes.L4}
	warmGPU.Autoscaler = &apitypes.Autoscaler{MinContainers: new(2), MaxContainers: new(2)}
	specs := []apitypes.WorkloadSpec{pinned, model, cards, warm, warmGPU}

	account(t, pool, ws, "free-v2", false)
	want := []string{
		"cards gpu_count: add a payment method",
		"model gpu_model: add a payment method",
		"pinned region_selection: upgrade to Team",
		"warm warm_floor: add a payment method",
		"warmgpu warm_floor: add a payment method",
	}
	if got := refusedBy(t, c, ws, specs...); !slices.Equal(got, want) {
		t.Fatalf("Free without a card refused\n%v\nwant\n%v", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
	var apps int
	if err := pool.QueryRow(t.Context(), "select count(*) from apps").Scan(&apps); err != nil || apps != 0 {
		t.Fatalf("a refused deploy left %d apps, %v", apps, err)
	}
	account(t, pool, ws, "free-v2", true)
	cards.Resources.GpuCount = new(6)
	if got := refusedBy(t, c, ws, cards); !slices.Equal(got, []string{"cards gpu_count: upgrade to Team"}) {
		t.Fatalf("6 GPUs on Free: %v", got)
	}
	if _, err := pool.Exec(t.Context(), "update billing_accounts set complimentary_since = now()"); err != nil {
		t.Fatal(err)
	}
	if got := refusedBy(t, c, ws, specs...); got != nil {
		t.Fatalf("complimentary refused %v", got)
	}
}

func TestDeployRefusesSecretsTheWorkspaceLacks(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	if _, err := pool.Exec(t.Context(), `insert into secrets (workspace_id, name, key_id, wrapped_key, nonce, ciphertext)
values ($1, 'HAVE', 'k', '\x00', '\x000000000000000000000000', '\x00')`, uuid.UUID(ws)); err != nil {
		t.Fatal(err)
	}
	spec := function("summarize")
	spec.Secrets = &[]string{"HAVE", "TOKEN", "API_KEY"}
	_, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Workloads: []apitypes.WorkloadSpec{spec}})
	var refused *RefusedError
	if !errors.As(err, &refused) || len(refused.Refusals) != 1 {
		t.Fatalf("missing secrets: %v", err)
	}
	if r := refused.Refusals[0]; r.Gate != apitypes.MissingSecrets || r.Message != "the workspace has no secret named TOKEN, API_KEY" {
		t.Fatalf("refusal %+v", r)
	}
}

// A devbox's root disk must hold its image's unpacked root.
func TestDeployRefusesARootDiskSmallerThanItsImage(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	// 2,560 frames of 4 MiB: a 10 GiB unpacked root.
	if _, err := pool.Exec(t.Context(), `
with image as (
    insert into images (digest, id, dockerfile, python_version, architecture, reference, ready_at)
    values (sha256('image'), 'img_000000000000000000000001', '', '3.12', 'amd64', 'registry/repo@sha256:1', now())
    returning digest
), ws as (insert into workspace_images (workspace_id, image_digest) select $1, digest from image),
layer as (
    insert into image_layers (id, blob_digest, diff_id, frames)
    values (gen_random_uuid(), 'sha256:' || repeat('a', 64), 'sha256:' || repeat('b', 64), 2560)
    returning id
)
insert into image_reference_layers (reference, position, layer_id) select 'registry/repo@sha256:1', 0, id from layer`, uuid.UUID(ws)); err != nil {
		t.Fatal(err)
	}
	box := devbox("box", 10<<30)
	box.Image.ImageId = new("img_000000000000000000000001")
	_, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Workloads: []apitypes.WorkloadSpec{box}})
	var refused *RefusedError
	if !errors.As(err, &refused) || len(refused.Refusals) != 1 {
		t.Fatalf("a 10 GiB root for a 10 GiB image: %v", err)
	}
	if r := refused.Refusals[0]; r.Gate != apitypes.DiskImage || r.Remedy != "set disk to at least 12 GiB" ||
		r.Message != "the root disk box of 10 GiB is too small for its image's unpacked root of up to 10 GiB" {
		t.Fatalf("refusal %+v", r)
	}
	// A root disk that exists at the size was seeded by an earlier start,
	// so its redeploy copies nothing.
	if _, err := pool.Exec(t.Context(), "insert into disks (workspace_id, name, size_bytes) values ($1, 'box', $2)", uuid.UUID(ws), int64(10<<30)); err != nil {
		t.Fatal(err)
	}
	if got := refusedBy(t, c, ws, box); got != nil {
		t.Fatalf("a redeploy of a seeded 10 GiB root: %v", got)
	}
	box.Disks = &[]apitypes.DiskMountSpec{{Name: "box", SizeBytes: 12 << 30, MountPath: "/"}}
	if got := refusedBy(t, c, ws, box); got != nil {
		t.Fatalf("a 12 GiB root: %v", got)
	}
}

// The plan reports each listed workload's refusals before anything builds.
// Only a devbox's root disk has a minimum.
func TestPlanReportsRefusalsByWorkload(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	account(t, pool, ws, "free-v2", true)
	root := func(name string, size int64) *[]apitypes.DiskMountSpec {
		return &[]apitypes.DiskMountSpec{{Name: name, SizeBytes: size, MountPath: "/"}}
	}
	plan, err := c.PlanDeployment(t.Context(), ws, "reports", apitypes.DeploymentPlanRequest{Workloads: []apitypes.DeploymentPlanWorkload{
		{Kind: apitypes.WorkloadKindFunction, Name: "summarize"},
		{Kind: apitypes.WorkloadKindPod, PodKind: new(apitypes.PodKindDevbox), Name: "box", Disks: root("box", 1<<30)},
	}})
	if err != nil {
		t.Fatal(err)
	}
	gates := map[string][]apitypes.DeploymentGate{}
	for _, item := range plan.Items {
		for _, r := range deref(item.Refusals) {
			gates[item.Name] = append(gates[item.Name], r.Gate)
		}
	}
	if len(gates) != 1 || !slices.Equal(gates["box"], []apitypes.DeploymentGate{apitypes.DiskMinimum, apitypes.DiskAllowance}) {
		t.Fatalf("plan refusals %v", gates)
	}
}

// A devbox root below the minimum is refused with the deploy's other
// refusals; any other workload's disk at / takes any size.
func TestDeployHoldsOnlyDevboxRootsToTheMinimum(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	if got := refusedBy(t, c, ws, devbox("box", 1<<30)); !slices.Equal(got, []string{"box disk_minimum: set disk to at least 10 GiB"}) {
		t.Fatalf("a 1 GiB devbox root: %v", got)
	}
	work := function("work")
	work.Disks = &[]apitypes.DiskMountSpec{{Name: "work", SizeBytes: 2 << 30, MountPath: "/"}}
	if got := refusedBy(t, c, ws, work); got != nil {
		t.Fatalf("a function's 2 GiB disk at /: %v", got)
	}
}

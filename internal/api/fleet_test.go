package api_test

import (
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
)

// administrator returns a token of a platform administrator.
func (e *env) administrator() string {
	e.t.Helper()
	if _, err := e.identity.CreateUser(e.t.Context(), "admin@example.com", true); err != nil {
		e.t.Fatal(err)
	}
	token, err := e.identity.CreateToken(e.t.Context(), "admin@example.com", "", "test")
	if err != nil {
		e.t.Fatal(err)
	}
	return token
}

// fleetHost is a platform cloud host row; an empty instance is a launch the
// provider refused.
type fleetHost struct {
	instance, phase, evidence string
	reserve, prepared         *string
	agent                     string
	online                    bool
}

func (e *env) fleetHost(h fleetHost) uuid.UUID {
	e.t.Helper()
	state, seen := "offline", (*time.Time)(nil)
	if h.online {
		now := time.Now()
		state, seen = "online", &now
	}
	evidence := h.evidence
	if evidence == "" {
		evidence = "unknown"
	}
	var id uuid.UUID
	if err := e.pool.QueryRow(e.t.Context(), `
insert into hosts (name, token_hash, state, last_seen_at, cpu_millis, memory_bytes, provider, kind, phase, region,
                   instance_type, market, instance_id, reserve_mode, image_evidence, prepared_agent_version, agent_version)
values ('lazycloud-m7i.xlarge', sha256(random()::text::bytea), $1, $2, 4000, 16::bigint << 30, 'aws', 'platform', $3, 'us-east-2',
        'm7i.xlarge', 'on_demand', nullif($4, ''), $5, $6, $7, $8)
returning id`, state, seen, h.phase, h.instance, h.reserve, evidence, h.prepared, h.agent).Scan(&id); err != nil {
		e.t.Fatal(err)
	}
	return id
}

func (e *env) fleet(token string) apitypes.FleetSummary {
	e.t.Helper()
	var out apitypes.FleetSummary
	if status := e.do("GET", "/v1/fleet", token, nil, &out); status != 200 {
		e.t.Fatalf("fleet summary: %d", status)
	}
	return out
}

func TestFleetReturnsThePublishedPlanUntilItExpires(t *testing.T) {
	ctx := t.Context()
	e := newEnv(t)
	admin := e.administrator()
	e.fleetHost(fleetHost{instance: "i-0serving", phase: "ready", online: true})
	e.fleetHost(fleetHost{instance: "i-0saved", phase: "stopped", reserve: ptr("hibernate"), evidence: "saved"})

	if plan := e.fleet(admin).Plan; plan != nil {
		t.Fatalf("fleet summary before any plan: %+v, want no plan", plan)
	}
	if result, err := e.compute.Plan(ctx, slog.New(slog.DiscardHandler)); err != nil || !result.Published {
		t.Fatalf("plan the fleet: %+v %v, want a published plan", result, err)
	}
	published, err := e.compute.PublishedPlan(ctx)
	if err != nil {
		t.Fatal(err)
	}
	plan := e.fleet(admin).Plan
	if plan == nil || len(plan.Markets) != len(published) || !plan.GeneratedAt.Equal(published[0].GeneratedAt) ||
		!plan.ExpiresAt.Equal(published[0].ExpiresAt) {
		t.Fatalf("fleet plan %+v, want the %d published markets", plan, len(published))
	}
	if first, second := plan.Markets[0], plan.Markets[1]; !first.Preemptible || first.GpuType != "" || second.Preemptible || second.GpuType != "" {
		t.Fatalf("markets start %+v, %+v; want Spot CPU, then on-demand CPU", first, second)
	}
	capacity := func(c compute.FleetCapacity) apitypes.FleetCapacity {
		return apitypes.FleetCapacity{CpuMillicores: c.CPUMillis, MemoryMib: c.MemoryBytes >> 20, GpuCount: c.GPUs}
	}
	host := apitypes.FleetCapacity{CpuMillicores: 4000, MemoryMib: 16 << 10}
	for _, want := range published {
		var got *apitypes.FleetMarket
		for i, m := range plan.Markets {
			if m.Preemptible == want.Preemptible && m.GpuType == want.GPUType {
				got = &plan.Markets[i]
			}
		}
		if got == nil {
			t.Fatalf("market %+v missing from %+v", want, plan.Markets)
		}
		if got.WarmFree != capacity(want.WarmFree) || got.WarmTarget != capacity(want.WarmTarget) ||
			got.ReserveReady != capacity(want.ReserveReady) || got.ReserveTarget != capacity(want.StoppedTarget) ||
			got.Allocated != capacity(want.Load) || got.Reason != string(want.Reason) || len(got.States) != len(want.States) {
			t.Errorf("market %s/%s: %+v, want the published %+v", map[bool]string{true: "spot", false: "on_demand"}[want.Preemptible], want.GPUType, got, want)
		}
		if want.Preemptible || want.GPUType != "" {
			continue
		}
		if got.ReserveReady != host || got.ReserveTarget.CpuMillicores == 0 || got.WarmFree != host {
			t.Errorf("on-demand CPU: reserve ready %+v of %+v, warm free %+v; want the hibernated host ready and the serving one free",
				got.ReserveReady, got.ReserveTarget, got.WarmFree)
		}
		states := map[apitypes.FleetState]int{}
		for _, s := range got.States {
			states[s.State] = s.Machines
		}
		if states[apitypes.FleetImageSaved] != 1 || states[apitypes.FleetServing] != 1 {
			t.Errorf("on-demand CPU states %+v, want one serving and one hibernated host", got.States)
		}
	}

	if _, err := e.pool.Exec(ctx, `update fleet_markets set generated_at = now() - interval '6 minutes', expires_at = now() - interval '1 minute'`); err != nil {
		t.Fatal(err)
	}
	if plan := e.fleet(admin).Plan; plan != nil {
		t.Fatalf("fleet summary with an expired plan: %+v, want no plan", plan)
	}
}

func TestFleetNodesListReserveStatesAndNeverARefusedLaunch(t *testing.T) {
	ctx := t.Context()
	e := newEnv(t)
	admin := e.administrator()
	if err := e.compute.PublishAgentRelease(ctx, compute.AgentRelease{
		Version: "2.0.0", SHA256: map[string]string{"amd64": strings.Repeat("a", 64)}, RolloutPercent: 100,
	}); err != nil {
		t.Fatal(err)
	}
	type want struct {
		state apitypes.FleetState
		ready bool
	}
	hibernate, stop := ptr("hibernate"), ptr("stop")
	current, old := ptr("2.0.0"), ptr("1.0.0")
	wants := map[uuid.UUID]want{
		e.fleetHost(fleetHost{instance: "i-0serving", phase: "ready", online: true, agent: "2.0.0"}):           {apitypes.FleetServing, true},
		e.fleetHost(fleetHost{instance: "i-0outdated", phase: "ready", online: true, agent: "1.0.0"}):          {apitypes.FleetServing, false},
		e.fleetHost(fleetHost{instance: "i-0preparing", phase: "preparing", reserve: hibernate, online: true}): {apitypes.FleetPreparing, false},
		e.fleetHost(fleetHost{instance: "i-0stopping", phase: "stopping", reserve: hibernate}):                 {apitypes.FleetStopping, false},
		e.fleetHost(fleetHost{instance: "i-0stopped", phase: "stopped", reserve: stop, prepared: current}):     {apitypes.FleetStopped, true},
		e.fleetHost(fleetHost{instance: "i-0unverified", phase: "stopped", reserve: hibernate, prepared: current}): {
			apitypes.FleetHibernateUnverified, true,
		},
		e.fleetHost(fleetHost{instance: "i-0saved", phase: "stopped", reserve: hibernate, evidence: "saved", prepared: old}): {
			apitypes.FleetImageSaved, false,
		},
		e.fleetHost(fleetHost{instance: "i-0nosave", phase: "stopped", reserve: hibernate, evidence: "failed", prepared: current}): {
			apitypes.FleetStopped, true,
		},
		e.fleetHost(fleetHost{instance: "i-0resuming", phase: "resuming"}):                  {apitypes.FleetStarting, false},
		e.fleetHost(fleetHost{instance: "i-0refreshing", phase: "resuming", reserve: stop}): {apitypes.FleetPreparing, false},
		e.fleetHost(fleetHost{instance: "i-0terminating", phase: "terminating"}):            {apitypes.FleetTerminating, false},
		e.fleetHost(fleetHost{instance: "i-0failed", phase: "failed"}):                      {apitypes.FleetFailed, false},
	}
	refused := e.fleetHost(fleetHost{phase: "failed"})
	requested := e.fleetHost(fleetHost{phase: "requested"})

	got := map[uuid.UUID]apitypes.FleetNode{}
	cursor := ""
	for pages := 0; ; pages++ {
		if pages > len(wants) {
			t.Fatal("fleet nodes do not end")
		}
		var page apitypes.FleetNodePage
		path := "/v1/fleet/nodes?limit=5"
		if cursor != "" {
			path += "&cursor=" + cursor
		}
		if status := e.do("GET", path, admin, nil, &page); status != 200 {
			t.Fatalf("GET %s: %d", path, status)
		}
		for _, n := range page.Nodes {
			if _, seen := got[n.Id]; seen {
				t.Fatalf("node %s listed twice", n.Id)
			}
			got[n.Id] = n
		}
		if page.NextCursor == nil {
			break
		}
		cursor = *page.NextCursor
	}
	for _, absent := range []uuid.UUID{refused, requested} {
		if n, ok := got[absent]; ok {
			t.Errorf("host without an instance listed: %+v", n)
		}
	}
	if r := e.fleet(admin).Release; r == nil || r.Complete || r.Phases["current"] != 1 || r.Phases["updating"] != 1 || r.Phases["reserve"] != 6 {
		t.Errorf("rollout %+v, want one host current, one updating and six reserves", r)
	}
	for id, w := range wants {
		n, ok := got[id]
		switch {
		case !ok:
			t.Errorf("host %s missing", id)
		case n.State != w.state || n.Ready != w.ready || n.InstanceId == nil || *n.InstanceId == "":
			t.Errorf("node %v: state %s ready %v, want %s ready %v", deref(n.InstanceId), n.State, n.Ready, w.state, w.ready)
		}
	}
}

// An emptied consolidating host that lost its connection is unavailable on
// Nodes, and the rollout counts it offline rather than on the release.
func TestFleetRolloutAndNodesAgreeOnAnEmptiedConsolidatingHost(t *testing.T) {
	ctx := t.Context()
	e := newEnv(t)
	admin := e.administrator()
	if err := e.compute.PublishAgentRelease(ctx, compute.AgentRelease{
		Version: "2.0.0", SHA256: map[string]string{"amd64": strings.Repeat("a", 64)}, RolloutPercent: 100,
	}); err != nil {
		t.Fatal(err)
	}
	host := e.fleetHost(fleetHost{instance: "i-0consolidated", phase: "ready", agent: "2.0.0"})
	if _, err := e.pool.Exec(ctx, `update hosts set capacity_state = 'draining', capacity_reason = 'consolidating' where id = $1`, host); err != nil {
		t.Fatal(err)
	}
	var page apitypes.FleetNodePage
	if status := e.do("GET", "/v1/fleet/nodes", admin, nil, &page); status != 200 || len(page.Nodes) != 1 ||
		page.Nodes[0].State != apitypes.FleetUnavailable {
		t.Fatalf("fleet nodes: %d %+v, want the host unavailable", status, page.Nodes)
	}
	if r := e.fleet(admin).Release; r == nil || r.Phases["current"] != 0 || r.Phases["offline"] != 1 {
		t.Fatalf("rollout %+v, want the host offline", r)
	}
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

package compute_test

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// fleetHost is a platform instance of a catalog type in us-east-2a with the
// usable capacity its agent advertises, launched an hour ago.
func fleetHost(t *testing.T, o owners, phase compute.Phase, market compute.Market, typ, instance string) compute.HostID {
	t.Helper()
	ct, ok := compute.CatalogTypeNamed(typ)
	if !ok {
		t.Fatalf("no catalog type %s", typ)
	}
	usable := ct.Usable(0)
	host := newHost(t, o.pool, hostSpec{
		Provider: compute.ProviderAWS, Phase: phase, Market: market, Region: "us-east-2", Zone: "us-east-2a", ZoneID: "use2-az1",
		InstanceID: instance, CPU: usable.CPUMillis, Memory: usable.MemoryBytes,
	})
	price, _ := ct.OnDemandMicros("us-east-2")
	run(t, o.pool, "update hosts set instance_type = $2, hourly_micros = $3, launched_at = now() - interval '1 hour' where id = $1",
		uuid.UUID(host), typ, price)
	return host
}

// stoppedReserve is a reserve stopped plainly, prepared for agent 1.0.0.
func stoppedReserve(t *testing.T, o owners, market compute.Market, typ, instance string) compute.HostID {
	t.Helper()
	host := fleetHost(t, o, compute.PhaseStopped, market, typ, instance)
	run(t, o.pool, `update hosts set state = 'offline', reserve_mode = 'stop', prepared_agent_version = '1.0.0', image_evidence = 'unavailable',
	                stopped_at = now() - interval '10 minutes' where id = $1`, uuid.UUID(host))
	return host
}

// idleHost is a serving on-demand host lightly used for an hour.
func idleHost(t *testing.T, o owners, typ, instance string) compute.HostID {
	t.Helper()
	host := fleetHost(t, o, compute.PhaseReady, compute.MarketOnDemand, typ, instance)
	run(t, o.pool, "update hosts set light_since = now() - interval '1 hour' where id = $1", uuid.UUID(host))
	return host
}

// staleMarkets makes the published plan old enough that the next pass
// plans reserves.
func staleMarkets(t *testing.T, o owners) {
	t.Helper()
	run(t, o.pool, "update fleet_markets set generated_at = now() - interval '61 seconds'")
}

type hostRow struct {
	Phase, CapacityState, CapacityReason string
	ReserveMode                          *string
	ResumeRequested                      bool
}

func hostRowOf(t *testing.T, o owners, host compute.HostID) hostRow {
	t.Helper()
	var r hostRow
	if err := o.pool.QueryRow(t.Context(), `
select phase, capacity_state, capacity_reason, reserve_mode, resume_requested_at is not null from hosts where id = $1`,
		uuid.UUID(host)).Scan(&r.Phase, &r.CapacityState, &r.CapacityReason, &r.ReserveMode, &r.ResumeRequested); err != nil {
		t.Fatal(err)
	}
	return r
}

func TestPendingWorkResumesAReadyReserveBeforeBuying(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	publish(t, o.compute)
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	reserve := stoppedReserve(t, o, compute.MarketOnDemand, "m7i.xlarge", "i-0000000000000f001")
	container := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"placement": {"preemptible": false}}`), 1000, gib)

	if r := planCapacity(t, o); r.Resumed != 1 || r.Requested != 0 {
		t.Fatalf("plan %+v, want the reserve resumed and nothing bought", r)
	}
	if h := hostRowOf(t, o, reserve); h.Phase != string(compute.PhaseResuming) || !h.ResumeRequested || h.ReserveMode != nil {
		t.Fatalf("reserve %+v, want a requested resume to serve", h)
	}
	if w := capacityWait(t, o, container); w != string(compute.WaitProvisioning) {
		t.Fatalf("container waits %q, want provisioning", w)
	}
	if got := scan[uuid.UUID](t, o.pool, "select capacity_host_id from containers where id = $1", container); got != uuid.UUID(reserve) {
		t.Fatalf("container waits for %s, want the resumed reserve", got)
	}
}

// Spot work resumes an on-demand reserve only while the on-demand reserves
// left behind still meet the stopped target this pass computes.
func TestSpotWorkResumesAnOnDemandReserveOnlyAboveTheTargetThisPassComputes(t *testing.T) {
	for _, c := range []struct {
		reserves int
		resumed  int
	}{{1, 0}, {2, 1}} {
		o := newOwners(t, fleetConfig(compute.Fleet{}))
		publish(t, o.compute)
		alice := newUser(t, o.pool, "alice@example.com")
		dev := newWorkspace(t, o.pool, "dev", alice)
		for n := range c.reserves {
			stoppedReserve(t, o, compute.MarketOnDemand, "m7i.2xlarge", "i-000000000000f1"+string(rune('0'+n))+"0")
		}
		pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{}`), 1000, gib)
		r := planCapacity(t, o)
		if r.Resumed != c.resumed || r.Requested != 1-c.resumed {
			t.Errorf("with %d on-demand reserves: plan %+v, want %d resumed", c.reserves, r, c.resumed)
		}
	}
}

// A host still launching is not running free room, so it never lets a
// serving host leave.
func TestPendingLaunchesNeverJustifyRetiringServingHosts(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	publish(t, o.compute)
	serving := idleHost(t, o, "m7i.2xlarge", "i-0000000000000f201")
	fleetHost(t, o, compute.PhaseRequested, compute.MarketOnDemand, "m7i.2xlarge", "")

	if r := plan(t, o); !r.Published {
		t.Fatalf("plan %+v, want a reserve pass", r)
	}
	if h := hostRowOf(t, o, serving); h.Phase != string(compute.PhaseReady) {
		t.Fatalf("serving host is %s while the only other host is launching, want ready", h.Phase)
	}
}

// An idle host the warm target can spare stops into the reserve while the
// reserve falls short, hibernating where its market hibernates, and its
// session is woken to ask the agent at once.
func TestAnIdleHostReturnsToTheReserveWhileTheReserveIsShort(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	publish(t, o.compute)
	a := idleHost(t, o, "m7i.2xlarge", "i-0000000000000f301")
	b := idleHost(t, o, "m7i.2xlarge", "i-0000000000000f302")
	run(t, o.pool, "update hosts set hibernation_configured = true where id = any($1)", []uuid.UUID{uuid.UUID(a), uuid.UUID(b)})
	conn, err := o.pool.Acquire(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Release()
	if _, err := conn.Exec(t.Context(), "listen lc_host"); err != nil {
		t.Fatal(err)
	}

	if r := plan(t, o); r.Returned != 1 || r.Drained != 0 {
		t.Fatalf("plan %+v, want one host returned to the reserve", r)
	}
	returned, kept := a, b
	if hostRowOf(t, o, a).Phase == string(compute.PhaseReady) {
		returned, kept = b, a
	}
	if h := hostRowOf(t, o, returned); h.Phase != string(compute.PhasePreparing) || h.ReserveMode == nil || *h.ReserveMode != "hibernate" {
		t.Fatalf("returned host %+v, want preparing to hibernate", h)
	}
	if h := hostRowOf(t, o, kept); h.Phase != string(compute.PhaseReady) {
		t.Fatalf("the warm target's host is %s, want ready", h.Phase)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
	defer cancel()
	n, err := conn.Conn().WaitForNotification(ctx)
	if err != nil || n.Payload != returned.String() {
		t.Fatalf("host notification %+v %v, want one naming the returned host", n, err)
	}
}

// Between reserve passes nothing leaves: retention, like reserve growth,
// waits for the pass that publishes the plan.
func TestRetirementWaitsForTheReservePass(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	publish(t, o.compute)
	for _, instance := range []string{"i-0000000000000f401", "i-0000000000000f402"} {
		host := idleHost(t, o, "m7i.2xlarge", instance)
		run(t, o.pool, "update hosts set hibernation_configured = true where id = $1", uuid.UUID(host))
	}
	if r := planCapacity(t, o); r.Returned != 0 || r.Drained != 0 || r.Requested != 0 {
		t.Fatalf("pass between reserve passes %+v, want nothing retired or bought", r)
	}
	staleMarkets(t, o)
	if r := plan(t, o); !r.Published || r.Returned != 1 {
		t.Fatalf("reserve pass %+v, want the plan published and one host returned", r)
	}
}

// A lightly used host's movable work drains onto the rest of its market,
// one host per market at a time; once it empties the market cools down and
// the host takes work again until retention decides.
func TestOneConsolidationPerMarketUntilItsHostEmpties(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	publish(t, o.compute)
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	release := newRelease(t, o.pool, dev, `{}`)
	containers := map[compute.HostID]uuid.UUID{}
	for _, instance := range []string{"i-0000000000000f501", "i-0000000000000f502", "i-0000000000000f503"} {
		host := idleHost(t, o, "m7i.4xlarge", instance)
		containers[host] = scan[uuid.UUID](t, o.pool, `
insert into containers (id, workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
values (uuidv7(interval '-1 hour'), $1, $2, 'ready', $3, 1, 1000, 1 << 30, now(), now()) returning id`, dev, release, uuid.UUID(host))
	}
	if r := plan(t, o); r.Cordoned != 1 {
		t.Fatalf("plan %+v, want one host consolidating", r)
	}
	moving := compute.HostID(scan[uuid.UUID](t, o.pool, "select consolidating_host from fleet_markets where market = 'on_demand:cpu'"))
	if h := hostRowOf(t, o, moving); h.CapacityState != string(compute.CapacityDraining) || h.CapacityReason != "consolidating" {
		t.Fatalf("consolidating host %+v, want cordoned", h)
	}
	if s := scan[string](t, o.pool, "select state from containers where id = $1", containers[moving]); s != "draining" {
		t.Fatalf("its container is %s, want draining", s)
	}
	staleMarkets(t, o)
	if r := plan(t, o); r.Cordoned != 0 {
		t.Fatalf("second reserve pass %+v, want no second consolidation in the market", r)
	}

	run(t, o.pool, "update containers set state = 'stopped', stop_reason = 'stopped', stopped_at = now() where id = $1", containers[moving])
	staleMarkets(t, o)
	plan(t, o)
	var host *uuid.UUID
	var cooldown time.Time
	if err := o.pool.QueryRow(t.Context(), "select consolidating_host, consolidation_cooldown_until from fleet_markets where market = 'on_demand:cpu'").
		Scan(&host, &cooldown); err != nil {
		t.Fatal(err)
	}
	if host != nil || time.Until(cooldown) < 14*time.Minute {
		t.Fatalf("after the host emptied: consolidating %v, cooldown until %s; want none and 15 minutes", host, cooldown)
	}
	if h := hostRowOf(t, o, moving); h.CapacityReason == "consolidating" {
		t.Fatalf("emptied host %+v, still cordoned", h)
	}
}

// Growth stops at sixteen actions per market and pass; the next pass
// continues.
func TestGrowthStopsAtSixteenActionsPerMarketAndPass(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{MaxHosts: 30}))
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	release := newRelease(t, o.pool, dev, `{"placement": {"preemptible": false}}`)
	for range 20 {
		pendingContainer(t, o.pool, dev, release, 40_000, 64*gib)
	}
	if r := planCapacity(t, o); r.Requested != 16 {
		t.Fatalf("first pass %+v, want 16 hosts", r)
	}
	if r := planCapacity(t, o); r.Requested != 4 {
		t.Fatalf("second pass %+v, want the 4 left", r)
	}
}

// A return to the reserve whose agent never answers fails after the limit
// and reconcile ends its instance; an agent update in flight waits.
func TestAReserveWhoseAgentNeverAnswersFails(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	stuck := fleetHost(t, o, compute.PhasePreparing, compute.MarketOnDemand, "m7i.large", "i-0000000000000f601")
	updating := fleetHost(t, o, compute.PhasePreparing, compute.MarketOnDemand, "m7i.large", "i-0000000000000f602")
	recent := fleetHost(t, o, compute.PhasePreparing, compute.MarketOnDemand, "m7i.large", "i-0000000000000f603")
	run(t, o.pool, "update hosts set phase_at = now() - interval '16 minutes', reserve_mode = 'stop' where id = any($1)",
		[]uuid.UUID{uuid.UUID(stuck), uuid.UUID(updating)})
	run(t, o.pool, "update hosts set updating_until = now() + interval '1 minute' where id = $1", uuid.UUID(updating))
	run(t, o.pool, "update hosts set reserve_mode = 'stop' where id = $1", uuid.UUID(recent))

	if r := planCapacity(t, o); r.Failed != 1 {
		t.Fatalf("plan %+v, want one reserve failed", r)
	}
	for host, want := range map[compute.HostID]compute.Phase{
		stuck: compute.PhaseFailed, updating: compute.PhasePreparing, recent: compute.PhasePreparing,
	} {
		phase, failure := hostPhase(t, o.pool, host)
		if phase != string(want) || (want == compute.PhaseFailed && (failure == nil || *failure != string(compute.FailureServiceLost))) {
			t.Errorf("host %s is %s (%v), want %s", host, phase, failure, want)
		}
	}
}

// A cold boot after a hibernation in the last day (P1) stops that type
// plainly in its region: a host that cannot hibernate does not return to a
// reserve that hibernates, so it drains.
func TestAColdBootAfterAHibernationStopsTheTypePlainly(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	publish(t, o.compute)
	for _, instance := range []string{"i-0000000000000f701", "i-0000000000000f702"} {
		host := idleHost(t, o, "m7i.2xlarge", instance)
		run(t, o.pool, "update hosts set hibernation_configured = true where id = $1", uuid.UUID(host))
	}
	run(t, o.pool, `insert into fleet_activations (kind, instance_type, region, seconds, outcome)
	                values ('resume', 'm7i.2xlarge', 'us-east-2', 95, 'cold_boot')`)
	if r := plan(t, o); r.Returned != 0 || r.Drained != 1 {
		t.Fatalf("plan %+v, want the idle host drained, not hibernated", r)
	}
}

// The snapshot reads the quota rooms and Spot prices their refresh loops
// write: a Spot quota with no room moves Spot buying to the next region.
func TestQuotaRoomsAndSpotPricesSteerPurchases(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{}`), 1000, gib)
	run(t, o.pool, `insert into fleet_quotas (region, quota_class, market, vcpus, observed_at) values ('us-east-2', 'standard', 'spot', 0, now())`)

	if r := planCapacity(t, o); r.Requested != 1 {
		t.Fatalf("plan %+v, want one host", r)
	}
	if got := requested(t, o); len(got) != 1 || !strings.Contains(got[0], "spot us-west-1") {
		t.Fatalf("requested %v, want Spot in us-west-1 while us-east-2 has no Spot quota", got)
	}
}

// Every reserve pass publishes each market's plan with a five-minute
// expiry and logs a market's decision only when it changed.
func TestThePassPublishesEachMarketAndLogsOnlyChangedDecisions(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{Networks: map[string]compute.Network{}}))
	var logs bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&logs, nil))
	pass := func() {
		t.Helper()
		if _, err := o.compute.Plan(t.Context(), logger); err != nil {
			t.Fatal(err)
		}
	}
	pass()
	if n := strings.Count(logs.String(), "fleet market plan"); n != 5 {
		t.Fatalf("%d decision lines, want one per market:\n%s", n, logs.String())
	}
	markets, err := o.compute.PublishedPlan(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(markets) != 5 {
		t.Fatalf("%d published markets, want Spot and on-demand CPU and three cards", len(markets))
	}
	for _, m := range markets {
		if d := m.ExpiresAt.Sub(m.GeneratedAt); d != 5*time.Minute {
			t.Errorf("market %+v expires %s after it was generated", m, d)
		}
		if m.GPUType == "" && (m.WarmTarget.CPUMillis != 2000 || m.Reason != compute.ReasonNoOffer) {
			t.Errorf("CPU market %+v, want the 2 vCPU warm floor short for want of an offer", m)
		}
	}
	short := scan[int](t, o.pool, "select count(*) from fleet_markets where pressure_since is not null")
	if short != 2 {
		t.Fatalf("%d markets under pressure, want the two CPU markets short of their warm floor", short)
	}
	logs.Reset()
	staleMarkets(t, o)
	pass()
	if strings.Contains(logs.String(), "fleet market plan") {
		t.Fatalf("an unchanged plan logged its decisions again:\n%s", logs.String())
	}
	var raw []byte
	if err := o.pool.QueryRow(t.Context(), "select plan from fleet_markets where market = 'spot:cpu'").Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var published map[string]any
	if err := json.Unmarshal(raw, &published); err != nil || published["decision"] == "" {
		t.Fatalf("published plan %s %v, want a decision", raw, err)
	}
}

// A pending container resumes a stopped reserve, the actuator starts it,
// the host says Hello, and placement gives it the container.
func TestAPendingContainerResumesAReserveThatJoinsAndTakesIt(t *testing.T) {
	o, _, ec2 := reserveFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	host := stoppedReserve(t, o, compute.MarketOnDemand, "m7i.xlarge", "i-0000000000000f801")
	ec2.add(fakeInstance{ID: "i-0000000000000f801", State: "stopped", Type: "m7i.xlarge"}, host.String())
	container := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"placement": {"preemptible": false}}`), 1000, gib)

	if r := planCapacity(t, o); r.Resumed != 1 {
		t.Fatalf("plan %+v, want the reserve resumed", r)
	}
	if r := actuate(t, o); r.Started != 1 {
		t.Fatalf("actuate %+v, want the reserve started", r)
	}
	usable, _ := compute.CatalogTypeNamed("m7i.xlarge")
	if _, err := o.compute.OpenSession(t.Context(), host, compute.SessionOpen{
		BootID: "boot-2", AgentVersion: "1.0.0",
		Capacity: compute.Capacity{CPUMillis: usable.Usable(0).CPUMillis, MemoryBytes: usable.Usable(0).MemoryBytes},
	}); err != nil {
		t.Fatal(err)
	}
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseReady) {
		t.Fatalf("resumed host is %s after its Hello, want ready", phase)
	}
	if n := place(t, o); n != 1 {
		t.Fatalf("placed %d, want the container", n)
	}
	if got := containerHost(t, o.pool, container); got == nil || *got != uuid.UUID(host) {
		t.Fatalf("container on %v, want the resumed reserve", got)
	}
}

// A connected account keeps no reserves: its idle host leaves after the
// idle timeout.
func TestAConnectedAccountsIdleHostLeavesAfterTheIdleTimeout(t *testing.T) {
	o, _, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	idle := idleHost(t, o, "m7i.large", "i-0000000000000f901")
	recent := idleHost(t, o, "m7i.large", "i-0000000000000f902")
	run(t, o.pool, "update hosts set kind = 'connection', connection_id = $1 where id = any($2)", conn.ID, []uuid.UUID{uuid.UUID(idle), uuid.UUID(recent)})
	run(t, o.pool, "update hosts set light_since = now() - interval '1 minute' where id = $1", uuid.UUID(recent))

	if r := planCapacity(t, o); r.Drained != 1 || r.Returned != 0 {
		t.Fatalf("plan %+v, want the idle connection host drained", r)
	}
	if phase, _ := hostPhase(t, o.pool, idle); phase != string(compute.PhaseDraining) {
		t.Fatalf("idle connection host is %s, want draining", phase)
	}
	if phase, _ := hostPhase(t, o.pool, recent); phase != string(compute.PhaseReady) {
		t.Fatalf("connection host idle for a minute is %s, want ready", phase)
	}
}

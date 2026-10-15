package compute

import (
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

// The plan tests use two shapes: m.small (8 CPU, 16 GiB) and m.large
// (32 CPU, 64 GiB, four smalls' usable capacity), in the standard quota
// class like the M family.
var (
	planSmall = CatalogType{Name: "m.small", Topology: twoPerCore(16), MemoryBytes: 16 * gib, prices: [4]int64{100_000, 100_000, 100_000, 100_000}}
	planLarge = CatalogType{Name: "m.large", Topology: twoPerCore(64), MemoryBytes: 64 * gib, prices: [4]int64{300_000, 300_000, 300_000, 300_000}}
	small     = planSmall.Usable(0)
	large     = planLarge.Usable(0)
	onDemand  = ReserveMarket{}
)

// planPolicy keeps warm and stopped floors in the on-demand CPU market and
// nothing anywhere else.
func planPolicy(warm, stopped FleetCapacity) Policy {
	p := DefaultPolicy()
	p.Spot = MarketReserve{}
	p.OnDemand = MarketReserve{Warm: HeadroomTarget{Floor: warm}, Stopped: HeadroomTarget{Floor: stopped}}
	p.GPU = nil
	return p
}

func planSnapshot(t *testing.T, hosts ...FleetHost) FleetSnapshot {
	in := offerInputs(t)
	in.Catalog = []CatalogType{planSmall, planLarge}
	return FleetSnapshot{Now: offerNow, Hosts: hosts, Offers: in, HostRoom: 100, ReserveRoom: 100}
}

func planHost(id byte, typ CatalogType, state FleetState) FleetHost {
	price, _ := typ.OnDemandMicros("us-east-2")
	h := FleetHost{
		ID: HostID{id}, InstanceType: typ.Name, Region: "us-east-2", Zone: "us-east-2a", ZoneID: "use2-az1",
		Market: MarketOnDemand, Usable: typ.Usable(0), State: state, Stoppable: true,
		HourlyMicros: ptr(price + rootDiskMicros("us-east-2", rootVolumeGiB, baselineMiBps) + 5000),
	}
	if h.reserve() {
		h.ReserveMode = ptr(ReserveStop)
	}
	return h
}

// idle makes a serving host one that became ready an hour ago and runs
// nothing.
func idle(h FleetHost) FleetHost {
	h.PhaseAt = offerNow.Add(-time.Hour)
	return h
}

func marketPlan(t *testing.T, plan FleetPlan, m ReserveMarket) MarketPlan {
	t.Helper()
	for _, mp := range plan.Markets {
		if mp.Market == m {
			return mp
		}
	}
	t.Fatalf("no plan for %s", m)
	return MarketPlan{}
}

func actionsOf(plan FleetPlan, kinds ...FleetActionKind) []FleetAction {
	var out []FleetAction
	for _, a := range plan.Actions {
		if slices.Contains(kinds, a.Kind) {
			out = append(out, a)
		}
	}
	return out
}

func boughtTypes(plan FleetPlan) []string {
	var names []string
	for _, a := range actionsOf(plan, ActionBuy) {
		names = append(names, a.Offer.Type.Name)
	}
	return names
}

// boughtForWork are the types bought for pending containers.
func boughtForWork(plan FleetPlan) []string {
	var names []string
	for _, a := range actionsOf(plan, ActionBuy) {
		if len(a.Containers) > 0 {
			names = append(names, a.Offer.Type.Name)
		}
	}
	return names
}

func hostsOf(actions []FleetAction) []HostID {
	var ids []HostID
	for _, a := range actions {
		ids = append(ids, *a.Host)
	}
	return ids
}

func TestPlanBuysTheLowerTotalCostForTheWarmTarget(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		target FleetCapacity
		want   []string
	}{
		{small, []string{"m.small"}},
		{small.Times(4), []string{"m.large"}},
		{large.Times(4), []string{"m.large", "m.large", "m.large", "m.large"}},
	} {
		plan := PlanFleet(planPolicy(c.target, FleetCapacity{}), planSnapshot(t))
		if got := boughtTypes(plan); !slices.Equal(got, c.want) {
			t.Errorf("target %+v bought %v", c.target, got)
		}
		if mp := marketPlan(t, plan, onDemand); !mp.Shortfall.Empty() {
			t.Errorf("target %+v left %+v short", c.target, mp.Shortfall)
		}
	}
}

// A serving host due an agent update takes no work until it runs the
// target, so pending work it would fit buys a host instead of waiting.
func TestPendingWorkBuysPastAHostDueAnAgentUpdate(t *testing.T) {
	t.Parallel()
	buys := func(due bool) int {
		h := planHost(1, planSmall, FleetServing)
		h.UpdateDue = due
		snapshot := planSnapshot(t, h)
		snapshot.Pending = []DemandGroup{{Need: Requirement{CPUMillis: 1000, MemoryBytes: gib}, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}}}}
		n := 0
		for _, a := range PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), snapshot).Actions {
			if a.Kind == ActionBuy {
				n++
			}
		}
		return n
	}
	if n := buys(false); n != 0 {
		t.Fatalf("bought %d hosts for work a serving host fits", n)
	}
	if n := buys(true); n != 1 {
		t.Fatalf("bought %d hosts for work only a host due an update fits, want 1", n)
	}
}

// A reserve stopped after its reclaim notice never resumes: it retires and
// holds none of the stopped target, which a reserve bought in its place
// takes up.
func TestPlanRetiresAnInterruptedReserveAndBuysItsPlace(t *testing.T) {
	t.Parallel()
	interrupted := planHost(1, planSmall, FleetStopped)
	interrupted.Protected = true
	plan := PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, interrupted))
	if got := hostsOf(actionsOf(plan, ActionRetireReserve)); !slices.Equal(got, []HostID{{1}}) || len(actionsOf(plan, ActionBuyReserve)) != 1 {
		t.Fatalf("actions %+v", plan.Actions)
	}
}

func TestPlanPendingCapacityNeverJustifiesRetiringReadyCapacity(t *testing.T) {
	t.Parallel()
	s := planSnapshot(t, idle(planHost(1, planSmall, FleetServing)), planHost(2, planLarge, FleetStarting))
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), s)
	mp := marketPlan(t, plan, onDemand)
	if len(actionsOf(plan, ActionDrain, ActionReturnToReserve)) > 0 || mp.WarmFree != small || mp.WarmPending != large {
		t.Fatalf("actions %+v free %+v pending %+v", plan.Actions, mp.WarmFree, mp.WarmPending)
	}
}

func TestPlanRetiresAPendingReserveBeforeTheReadyOneItWouldReplace(t *testing.T) {
	t.Parallel()
	ready := planHost(1, planSmall, FleetStopped)
	pending := planHost(2, planSmall, FleetPreparing)
	plan := PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, ready, pending))
	mp := marketPlan(t, plan, onDemand)
	if got := hostsOf(actionsOf(plan, ActionRetireReserve)); !slices.Equal(got, []HostID{{2}}) || mp.ReserveReady != small {
		t.Fatalf("retired %v ready %+v", got, mp.ReserveReady)
	}
}

func TestPlanAResumedReserveCannotCoverRetiringTheLastReadyOne(t *testing.T) {
	t.Parallel()
	cheap := planHost(1, planSmall, FleetStopped)
	cheap.HourlyMicros = ptr(int64(10))
	preparing := planHost(2, planSmall, FleetPreparing)
	keep := planHost(3, planSmall, FleetStopped)
	keep.HourlyMicros = ptr(int64(20))
	plan := PlanFleet(planPolicy(small, small), planSnapshot(t, cheap, preparing, keep))
	mp := marketPlan(t, plan, onDemand)
	if got := hostsOf(actionsOf(plan, ActionResume)); !slices.Equal(got, []HostID{{1}}) {
		t.Fatalf("resumed %v", got)
	}
	if retired := hostsOf(actionsOf(plan, ActionRetireReserve)); slices.Contains(retired, HostID{3}) || mp.ReserveReady != small {
		t.Fatalf("retired %v ready %+v", retired, mp.ReserveReady)
	}
}

func TestSpotWorkBorrowsAnOnDemandReserveOnlyAboveItsTarget(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		reserves int
		target   FleetCapacity
		borrows  bool
	}{
		{1, small, false},
		{3, small.Times(3), false},
		{3, small.Times(2), true},
	} {
		var hosts []FleetHost
		for i := range c.reserves {
			hosts = append(hosts, planHost(byte(i+1), planSmall, FleetStopped))
		}
		s := planSnapshot(t, hosts...)
		s.Pending = []DemandGroup{{Need: Requirement{Preemptible: true, CPUMillis: 4000, MemoryBytes: 4 * gib}, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}}}}
		plan := PlanFleet(planPolicy(FleetCapacity{}, c.target), s)
		if borrowed := len(actionsOf(plan, ActionResume)) == 1; borrowed != c.borrows {
			t.Errorf("%d reserves, target %+v: actions %+v", c.reserves, c.target, plan.Actions)
		}
	}
}

// An on-demand host whose work could all move starts handing it back once
// a Spot offer can host it and no Spot pool of its region cools after a
// refusal, and lends again once no Spot offer can. A host that runs work
// that cannot move lends its room, even once it hands back, and an empty
// one never starts handing back.
func TestOnDemandHostsHandSpotWorkBackWhileSpotCanHostIt(t *testing.T) {
	t.Parallel()
	quoted := func(s FleetSnapshot) FleetSnapshot {
		s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: planSmall.Name, HourlyMicros: 30_000, ObservedAt: offerNow})
		return s
	}
	moves := []Requirement{{Preemptible: true, CPUMillis: 2000, MemoryBytes: 2 * gib}}
	moving := planHost(1, planSmall, FleetServing)
	moving.Load, moving.Moves, moving.Containers = cpuGiB(2000, 2), moves, 1
	kept := planHost(2, planSmall, FleetServing)
	kept.Load, kept.Moves, kept.Stays, kept.Containers = cpuGiB(4000, 4), moves, true, 2
	empty := planHost(3, planSmall, FleetServing)
	p := planPolicy(FleetCapacity{}, FleetCapacity{})

	s := quoted(planSnapshot(t, moving, kept, empty))
	need, waiting := pendingOne(Requirement{Preemptible: true, CPUMillis: 1000, MemoryBytes: gib}, nil)
	s.Pending = []DemandGroup{need}
	plan := PlanFleet(p, s)
	if back := actionsOf(plan, ActionHandBack); !slices.Equal(hostsOf(back), []HostID{moving.ID}) || len(actionsOf(plan, ActionLend)) > 0 {
		t.Fatalf("with Spot quoted: %+v", plan.Actions)
	}
	if w := waitOf(t, plan, waiting); w.Wait != nil {
		t.Fatalf("Spot-tolerant work waits %+v, want the room the kept host lends", w)
	}

	cooling := quoted(planSnapshot(t, moving))
	cooling.Offers.Cooldowns = []OfferCooldown{{Region: "us-east-2", ZoneID: "use2-az2", InstanceType: planLarge.Name, Market: MarketSpot, Until: offerNow.Add(time.Minute)}}
	if plan := PlanFleet(p, cooling); len(actionsOf(plan, ActionHandBack)) > 0 {
		t.Fatalf("while a Spot pool of the region cools: %+v", plan.Actions)
	}

	moving.HandsBack = true
	if plan := PlanFleet(p, cooling); len(plan.Actions) > 0 {
		t.Fatalf("handing back while Spot still hosts the work: %+v", plan.Actions)
	}
	if plan := PlanFleet(p, planSnapshot(t, moving, kept)); !slices.Equal(hostsOf(actionsOf(plan, ActionLend)), []HostID{moving.ID}) {
		t.Fatalf("once no Spot offer is left: %+v", plan.Actions)
	}
	kept.HandsBack = true
	if plan := PlanFleet(p, quoted(planSnapshot(t, kept))); !slices.Equal(hostsOf(actionsOf(plan, ActionLend)), []HostID{kept.ID}) {
		t.Fatalf("a host handing back that runs work that cannot move: %+v", plan.Actions)
	}
}

func pendingOne(need Requirement, host *HostID) (DemandGroup, uuid.UUID) {
	id := uuid.New()
	return DemandGroup{Need: need, Containers: []PendingContainer{{ID: id, Since: offerNow, Host: host}}}, id
}

func waitOf(t *testing.T, plan FleetPlan, id uuid.UUID) ContainerWait {
	t.Helper()
	for _, w := range plan.Waits {
		if w.Container == id {
			return w
		}
	}
	t.Fatalf("no wait for %s", id)
	return ContainerWait{}
}

func TestPlanPlacesDemandOnReadyRoomThenStartingHostsThenReservesThenPurchases(t *testing.T) {
	t.Parallel()
	need := Requirement{CPUMillis: 4000, MemoryBytes: 4 * gib}
	p := planPolicy(FleetCapacity{}, FleetCapacity{})

	s := planSnapshot(t, planHost(1, planSmall, FleetServing))
	g, id := pendingOne(need, nil)
	s.Pending = []DemandGroup{g}
	if plan := PlanFleet(p, s); len(plan.Actions) > 0 || waitOf(t, plan, id).Wait != nil {
		t.Fatalf("ready room: %+v %+v", plan.Actions, waitOf(t, plan, id))
	}

	s = planSnapshot(t, planHost(1, planSmall, FleetStarting), planHost(2, planSmall, FleetStarting))
	g, id = pendingOne(need, ptr(HostID{2}))
	s.Pending = []DemandGroup{g}
	if w := waitOf(t, PlanFleet(p, s), id); *w.Wait != WaitProvisioning || *w.Host != (HostID{2}) {
		t.Fatalf("the host bought for it: %+v", w)
	}

	s = planSnapshot(t, planHost(1, planSmall, FleetStopped))
	g, id = pendingOne(need, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(p, s)
	if w := waitOf(t, plan, id); len(plan.Actions) != 1 || plan.Actions[0].Kind != ActionResume ||
		!slices.Equal(plan.Actions[0].Containers, []uuid.UUID{id}) || *w.Host != (HostID{1}) {
		t.Fatalf("reserve: %+v %+v", plan.Actions, w)
	}

	s = planSnapshot(t)
	g, id = pendingOne(need, nil)
	s.Pending = []DemandGroup{g}
	plan = PlanFleet(p, s)
	if w := waitOf(t, plan, id); len(plan.Actions) != 1 || plan.Actions[0].Kind != ActionBuy || *w.Wait != WaitProvisioning || *w.Action != 0 {
		t.Fatalf("purchase: %+v %+v", plan.Actions, w)
	}

	s.HostRoom = 0
	plan = PlanFleet(p, s)
	if w := waitOf(t, plan, id); len(plan.Actions) > 0 || *w.Wait != WaitLimit || marketPlan(t, plan, onDemand).Reason != WaitLimit {
		t.Fatalf("fleet limit: %+v %+v", plan.Actions, w)
	}
}

func TestPlanCoversABatchOfContainersTogether(t *testing.T) {
	t.Parallel()
	s := planSnapshot(t)
	s.Offers.Catalog = FleetCatalog()
	need := Requirement{CPUMillis: 3000, MemoryBytes: 4 * gib}
	s.Pending = []DemandGroup{{Need: need, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}, {ID: uuid.New(), Since: offerNow}}}}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	if got := boughtTypes(plan); !slices.Equal(got, []string{"c6a.4xlarge"}) || len(plan.Actions[0].Containers) != 2 {
		t.Fatalf("two 3 CPU containers: %+v", plan.Actions)
	}
}

func TestPlanCapsGrowthPerMarketAndPass(t *testing.T) {
	t.Parallel()
	plan := PlanFleet(planPolicy(large.Times(20), FleetCapacity{}), planSnapshot(t))
	mp := marketPlan(t, plan, onDemand)
	if n := len(actionsOf(plan, ActionBuy)); n != 16 || mp.Reason != WaitDeferred || mp.Shortfall.Empty() {
		t.Fatalf("%d buys, reason %q, short %+v", n, mp.Reason, mp.Shortfall)
	}
}

// A reserve runs while it is prepared, so buying one takes host room: a
// full fleet buys none, however short the reserve.
func TestPlanBuysReservesOnlyWithinTheHostLimit(t *testing.T) {
	t.Parallel()
	for _, room := range []int{0, 100} {
		s := planSnapshot(t)
		s.HostRoom = room
		plan := PlanFleet(planPolicy(FleetCapacity{}, small.Times(2)), s)
		if n := len(actionsOf(plan, ActionBuyReserve)); (n > 0) != (room > 0) {
			t.Errorf("host room %d: %d reserves bought", room, n)
		}
	}
}

func TestAnyGPUDemandGoesToAReservedCardTheFleetHoldsThenTheCheapestPerCard(t *testing.T) {
	t.Parallel()
	need := Requirement{GPUs: []string{GPUAny}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 8 * gib}
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.GPU = DefaultPolicy().GPU
	loaded := func(typ string, model string) FleetHost {
		h := planHost(1, mustType(t, typ), FleetServing)
		h.GPU, h.Load = model, h.Usable
		return h
	}
	for _, c := range []struct {
		name  string
		hosts []FleetHost
		want  string
	}{
		{"no stock", nil, "g4dn.xlarge"},
		{"L4 in stock", []FleetHost{loaded("g6.2xlarge", "L4")}, "g6.2xlarge"},
		{"L40S is not a reserved card", []FleetHost{loaded("g6e.4xlarge", "L40S")}, "g4dn.xlarge"},
	} {
		s := planSnapshot(t, c.hosts...)
		s.Offers.Catalog = FleetCatalog()
		g, _ := pendingOne(need, nil)
		s.Pending = []DemandGroup{g}
		plan := PlanFleet(p, s)
		if got := boughtForWork(plan); !slices.Equal(got, []string{c.want}) {
			t.Errorf("%s: bought %v", c.name, got)
		}
	}
}

func TestGPUWorkFallsThroughToTheModelItListsNext(t *testing.T) {
	t.Parallel()
	s := planSnapshot(t)
	s.Offers.Catalog = FleetCatalog()
	g, _ := pendingOne(Requirement{GPUs: []string{"H100", "L4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 8 * gib}, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	// Both H100 types fail the margin, so the L4 serves.
	if buys := actionsOf(plan, ActionBuy); len(buys) != 1 || buys[0].Offer.Type.GPU != "L4" || buys[0].Market != (ReserveMarket{GPU: "L4"}) {
		t.Fatalf("actions %+v", plan.Actions)
	}
}

func TestPlanDrainsAOneTimeSpotHostInsteadOfStoppingIt(t *testing.T) {
	t.Parallel()
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.Spot = MarketReserve{Stopped: HeadroomTarget{Floor: small}}
	h := idle(planHost(1, planSmall, FleetServing))
	h.Market = MarketSpot
	for _, stoppable := range []bool{false, true} {
		h.Stoppable = stoppable
		plan := PlanFleet(p, planSnapshot(t, h))
		if returned := len(actionsOf(plan, ActionReturnToReserve)) == 1; returned != stoppable || (!stoppable && len(actionsOf(plan, ActionDrain)) != 1) {
			t.Errorf("stoppable %v: %+v", stoppable, plan.Actions)
		}
	}
}

// planFast is a small shape that hibernates; planRoomy can hibernate but
// has more RAM than a reserve hibernates.
var (
	planFast  = CatalogType{Name: "fast", Topology: twoPerCore(16), MemoryBytes: 16 * gib, Hibernates: true, prices: [4]int64{100_000, 100_000, 100_000, 100_000}}
	planRoomy = CatalogType{Name: "roomy", Topology: twoPerCore(16), MemoryBytes: 64 * gib, Hibernates: true, prices: [4]int64{200_000, 200_000, 200_000, 200_000}}
)

// Headroom and reserves follow the work: while a market's work waits
// without a host coming for it, as it does in an open arrival batch, the
// market buys neither.
func TestPlanKeepsHeadroomGrowthOutOfDemand(t *testing.T) {
	t.Parallel()
	s := planSnapshot(t)
	g, _ := pendingOne(Requirement{CPUMillis: 1000, MemoryBytes: gib}, nil)
	s.Pending = []DemandGroup{g}
	s.BatchWait = time.Second
	plan := PlanFleet(planPolicy(large, small), s)
	if mp := marketPlan(t, plan, onDemand); len(plan.Actions) > 0 || mp.Reason != WaitBatch {
		t.Errorf("actions %+v, reason %q; want nothing bought while the work waits", plan.Actions, mp.Reason)
	}
}

// Pending work that fits ready room takes it, so that room is no longer
// headroom: an idle host the warm target needs once the work lands stays.
func TestPlanCountsRoomPendingWorkTakesAgainstTheWarmTarget(t *testing.T) {
	t.Parallel()
	need := Requirement{CPUMillis: 6000, MemoryBytes: 8 * gib}
	s := planSnapshot(t, planHost(1, planSmall, FleetServing), idle(planHost(2, planSmall, FleetServing)))
	g, _ := pendingOne(need, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(planPolicy(cpuGiB(4000, 8), FleetCapacity{}), s)
	if mp := marketPlan(t, plan, onDemand); len(plan.Actions) > 0 || mp.WarmFree != small.Times(2).Minus(reservedShape(need)) {
		t.Fatalf("actions %+v, warm free %+v", plan.Actions, mp.WarmFree)
	}
}

func TestPlanReleasesTheCostliestIdleHostFirst(t *testing.T) {
	t.Parallel()
	dear := planSmall
	dear.Name, dear.prices = "m.dear", [4]int64{500_000, 500_000, 500_000, 500_000}
	costly := idle(planHost(1, dear, FleetServing))
	cheap := idle(planHost(2, planSmall, FleetServing))
	s := planSnapshot(t, costly, cheap)
	s.Offers.Catalog = append(s.Offers.Catalog, dear)
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), s)
	if got := hostsOf(plan.Actions); !slices.Equal(got, []HostID{{1}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("actions %+v", plan.Actions)
	}
}

// At zero load the warm floor converges on the cheapest host that meets it:
// an idle c6a.2xlarge ($0.32/h) is replaced by an m7i.large ($0.12/h) and
// leaves once the replacement serves. A host running work is not replaced,
// nor one whose replacement EC2 refused within the cost horizon.
func TestPlanWarmFloorConvergesOnTheCheapestHost(t *testing.T) {
	t.Parallel()
	p := DefaultPolicy()
	p.Spot, p.GPU, p.OnDemand = MarketReserve{}, nil, MarketReserve{Warm: p.OnDemand.Warm}
	snapshot := func(hosts ...FleetHost) FleetSnapshot {
		s := planSnapshot(t, hosts...)
		s.Offers.Catalog = FleetCatalog()
		return s
	}
	big := idle(planHost(1, mustType(t, "c6a.2xlarge"), FleetServing))
	plan := PlanFleet(p, snapshot(big))
	if moves := actionsOf(plan, ActionRightsize); len(plan.Actions) != 1 || len(moves) != 1 || *moves[0].Host != big.ID ||
		moves[0].Offer.Type.Name != "m7i.large" {
		t.Fatalf("idle c6a.2xlarge: %+v", plan.Actions)
	}
	if plan := PlanFleet(p, snapshot(big, planHost(2, mustType(t, "m7i.large"), FleetStarting))); len(plan.Actions) > 0 {
		t.Fatalf("while the replacement starts: %+v", plan.Actions)
	}
	plan = PlanFleet(p, snapshot(big, idle(planHost(2, mustType(t, "m7i.large"), FleetServing))))
	if got := hostsOf(plan.Actions); !slices.Equal(got, []HostID{{1}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("once it serves: %+v", plan.Actions)
	}
	busy := planHost(1, mustType(t, "c6a.2xlarge"), FleetServing)
	busy.Load, busy.Containers = cpuGiB(1000, 1), 1
	if plan := PlanFleet(p, snapshot(busy)); len(plan.Actions) > 0 {
		t.Fatalf("a serving host was replaced: %+v", plan.Actions)
	}
	refused := big
	refused.RightsizeRefusedAt = ptr(offerNow.Add(-p.CostHorizon + time.Minute))
	if plan := PlanFleet(p, snapshot(refused)); len(plan.Actions) > 0 {
		t.Fatalf("a host whose replacement EC2 refused within the cost horizon: %+v", plan.Actions)
	}
}

// An idle host goes back to the reserve while the reserve is short: an
// host of at most 32 GiB launched able to hibernate does, any other
// stops plainly. Once the reserve is held, it drains.
func TestPlanReturnsALeavingHostToTheReserveWhileTheReserveFallsShort(t *testing.T) {
	t.Parallel()
	p := planPolicy(FleetCapacity{}, small)
	p.Spot = MarketReserve{Stopped: HeadroomTarget{Floor: small}}
	hibernating := func(typ CatalogType, market Market) FleetHost {
		h := idle(planHost(1, typ, FleetServing))
		h.Market, h.HibernationConfigured = market, true
		return h
	}
	for _, c := range []struct {
		name string
		host FleetHost
		mode ReserveMode
	}{
		{"plain", idle(planHost(1, planSmall, FleetServing)), ReserveStop},
		{"on-demand, hibernation configured", hibernating(planFast, MarketOnDemand), ReserveHibernate},
		{"on-demand, 64 GiB", hibernating(planRoomy, MarketOnDemand), ReserveStop},
		{"Spot, hibernation configured", hibernating(planFast, MarketSpot), ReserveHibernate},
	} {
		s := planSnapshot(t, c.host)
		s.Offers.Catalog = append(s.Offers.Catalog, planFast, planRoomy)
		plan := PlanFleet(p, s)
		if got := actionsOf(plan, ActionReturnToReserve); len(got) != 1 || *got[0].Mode != c.mode || len(actionsOf(plan, ActionDrain)) > 0 {
			t.Errorf("%s: %+v", c.name, plan.Actions)
		}
	}
	s := planSnapshot(t, idle(planHost(1, planSmall, FleetServing)), planHost(2, planSmall, FleetStopped))
	if plan := PlanFleet(p, s); !slices.Equal(hostsOf(plan.Actions), []HostID{{1}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("reserve already held: %+v", plan.Actions)
	}
}

// Headroom that resumes a reserve on its own takes one whose absence
// leaves a reserve that fits the largest shape, though it is not the
// smallest.
func TestHeadroomResumesAReserveThatLeavesTheLargeOneStopped(t *testing.T) {
	t.Parallel()
	narrow := CatalogType{Name: "narrow", Topology: twoPerCore(8), MemoryBytes: 32 * gib, prices: [4]int64{90_000, 90_000, 90_000, 90_000}}
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.OnDemand = MarketReserve{Warm: HeadroomTarget{Floor: cpuGiB(1000, 4)}, FitLargest: true}
	p.LargestShape.Default = cpuGiB(8000, 8)
	large := planHost(1, planSmall, FleetStopped)
	s := planSnapshot(t, large, planHost(2, narrow, FleetStopped))
	s.Offers.Catalog = append(s.Offers.Catalog, narrow)
	plan := PlanFleet(p, s)
	if got := hostsOf(actionsOf(plan, ActionResume)); !slices.Equal(got, []HostID{{2}}) {
		t.Fatalf("resumed %v, want the reserve that leaves the large one stopped: %+v", got, plan.Actions)
	}
}

// Headroom resumes a reserve only when running it costs no more than
// buying for it, and never the reserve that fits the largest shape
// when no other would: an idle fleet whose only reserve has 16 CPUs buys a
// small host for its 1-CPU slot and keeps the reserve stopped.
func TestHeadroomBuysASmallHostRatherThanResumeTheLargeReserve(t *testing.T) {
	t.Parallel()
	p := DefaultPolicy()
	p.Spot, p.GPU = MarketReserve{}, nil
	reserve := planHost(1, mustType(t, "c6a.4xlarge"), FleetStopped)
	reserve.ReserveMode, reserve.HibernationConfigured = ptr(ReserveHibernate), true
	s := planSnapshot(t, reserve)
	s.Offers.Catalog = FleetCatalog()
	plan := PlanFleet(p, s)
	bought := actionsOf(plan, ActionBuy)
	if len(actionsOf(plan, ActionResume, ActionRetireReserve)) > 0 || len(bought) != 1 || bought[0].Offer.Type.Name != "m7i.large" {
		t.Fatalf("actions %+v, want an m7i.large bought for the slot and the reserve kept stopped", plan.Actions)
	}
}

// A market without a stopped floor holds its largest recent shape: on-demand
// T4 work, running or done within the window, keeps a stopped target of
// one card.
func TestRecentCardWorkKeepsAStoppedTargetOfOneCard(t *testing.T) {
	t.Parallel()
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.GPU = DefaultPolicy().GPU
	h := planHost(1, mustType(t, "g4dn.xlarge"), FleetServing)
	h.GPU, h.Load, h.Containers = "T4", FleetCapacity{CPUMillis: 2000, MemoryBytes: 8 * gib, GPUs: 1}, 1
	for _, running := range []bool{true, false} {
		s := planSnapshot(t)
		if running {
			s.Hosts = []FleetHost{h}
		}
		s.Offers.Catalog = FleetCatalog()
		s.Recent = map[ReserveMarket]FleetCapacity{{GPU: "T4"}: h.Load}
		if target := marketPlan(t, PlanFleet(p, s), ReserveMarket{GPU: "T4"}).StoppedTarget; target.GPUs != 1 {
			t.Errorf("running %t: T4 stopped target %+v, want one card", running, target)
		}
	}
}

// While load sets the Spot market's stopped target, a reserve bought for a
// small shortfall still holds the target up to the default largest shape,
// the size that hibernates, so a target that grows a little each pass is
// held by a few large reserves.
func TestALoadedSpotMarketBuysLargeReserves(t *testing.T) {
	t.Parallel()
	p := DefaultPolicy()
	p.OnDemand, p.GPU, p.Spot.Warm = MarketReserve{}, nil, HeadroomTarget{}
	p.Spot.Stopped.LoadPercent = 50
	spot := ReserveMarket{Preemptible: true}
	host := func(id byte, typ string, state FleetState) FleetHost {
		h := planHost(id, mustType(t, typ), state)
		h.Market = MarketSpot
		if state == FleetServing {
			h.Load, h.Containers = h.Usable, 16
		}
		return h
	}
	// 48 busy CPU keep a 24 CPU stopped target; the reserves hold 20.
	s := planSnapshot(t, host(1, "c6a.8xlarge", FleetServing), host(2, "c6a.8xlarge", FleetServing), host(3, "c6a.8xlarge", FleetServing),
		host(4, "c6a.8xlarge", FleetStopped), host(5, "c6a.2xlarge", FleetStopped))
	s.Offers.Catalog = FleetCatalog()
	for _, typ := range s.Offers.Catalog {
		s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: typ.Name, HourlyMicros: 10_000 * typ.VCPUs(), ObservedAt: offerNow})
	}
	plan := PlanFleet(p, s)
	bought := actionsOf(plan, ActionBuyReserve)
	if len(bought) == 0 {
		t.Fatalf("actions %+v, reason %q; want a reserve bought", plan.Actions, marketPlan(t, plan, spot).Reason)
	}
	for _, a := range bought {
		if !a.Offer.Usable.Covers(p.LargestShape.Default) {
			t.Errorf("bought reserve %s holds %+v, want at least %+v", a.Offer.Key(), a.Offer.Usable, p.LargestShape.Default)
		}
	}
}

// Load is the market's whose host runs it: Spot-tolerant work borrowing an
// on-demand host is the on-demand market's, so that host is held once the
// work ends. CPU work packed into a GPU host is the CPU Spot market's: it
// never holds the GPU host.
func TestLoadIsTheMarketsWhoseHostRunsItExceptPackedCPUWork(t *testing.T) {
	t.Parallel()
	p := DefaultPolicy()
	h := planHost(1, mustType(t, "c6a.4xlarge"), FleetServing)
	h.Load, h.Containers = cpuGiB(6000, 12), 3
	gpu := planHost(2, mustType(t, "g6.xlarge"), FleetServing)
	gpu.GPU, gpu.Market = "L4", MarketSpot
	gpu.Load, gpu.Packed, gpu.Containers = FleetCapacity{CPUMillis: 1250, MemoryBytes: 2 * gib, GPUs: 1}, cpuGiB(250, 1), 2
	plan := PlanFleet(p, planSnapshot(t, h, gpu))
	if spot := marketPlan(t, plan, ReserveMarket{Preemptible: true}).Load; spot != cpuGiB(250, 1) {
		t.Errorf("Spot load %+v, want the packed CPU work", spot)
	}
	if od := marketPlan(t, plan, onDemand).Load; od != cpuGiB(6000, 12) {
		t.Errorf("on-demand load %+v, want all the work on the on-demand host", od)
	}
	if l4 := marketPlan(t, plan, ReserveMarket{Preemptible: true, GPU: "L4"}).Load; l4 != (FleetCapacity{CPUMillis: 1000, MemoryBytes: gib, GPUs: 1}) {
		t.Errorf("L4 Spot load %+v, want the GPU work alone", l4)
	}
}

// A large idle host that holds the warm floor does not stand in for the
// reserve: the floor never lapses, so the market buys a reserve that fits
// the largest shape.
func TestALargeWarmHostDoesNotStandInForTheReserve(t *testing.T) {
	t.Parallel()
	p := DefaultPolicy()
	p.Spot, p.GPU = MarketReserve{}, nil
	big := mustType(t, "c6a.8xlarge")
	p.OnDemand = MarketReserve{Warm: HeadroomTarget{Floor: cpuGiB(1000, 4)}, Stopped: HeadroomTarget{Floor: cpuGiB(1000, 4)}, FitLargest: true}
	warm := idle(planHost(1, big, FleetServing))
	warm.RightsizeRefusedAt = ptr(offerNow)
	s := planSnapshot(t, warm)
	s.Offers.Catalog = FleetCatalog()
	s.Recent = map[ReserveMarket]FleetCapacity{onDemand: cpuGiB(12000, 24)}
	plan := PlanFleet(p, s)
	if len(actionsOf(plan, ActionBuyReserve)) == 0 {
		t.Fatalf("actions %+v, reason %q; want a reserve bought", plan.Actions, marketPlan(t, plan, onDemand).Reason)
	}
}

// A reserve resumed for work holds headroom after the hosts that serve
// it: busy with spare room, it does not push the idle warm host out, so it
// can go back into the reserve once its work ends.
func TestAResumedReserveLeavesTheWarmSlotsToTheWarmHost(t *testing.T) {
	t.Parallel()
	p := DefaultPolicy()
	p.Spot, p.GPU, p.OnDemand = MarketReserve{}, nil, MarketReserve{Warm: p.OnDemand.Warm}
	warm := idle(planHost(1, mustType(t, "m7i.large"), FleetServing))
	resumed := planHost(2, mustType(t, "c6a.4xlarge"), FleetServing)
	resumed.Load, resumed.Containers, resumed.Slept = cpuGiB(4000, 8), 1, true
	s := planSnapshot(t, warm, resumed)
	s.Offers.Catalog = FleetCatalog()
	if plan := PlanFleet(p, s); len(plan.Actions) > 0 {
		t.Fatalf("actions %+v, want the warm host kept", plan.Actions)
	}
}

// A rightsize's replacement holds the floor once it serves, even when
// work landed on the host it replaces meanwhile: the replacement stays,
// and the replaced host leaves once its work ends.
func TestARightsizeReplacementKeepsTheSlotWhenWorkLandsOnTheReplacedHost(t *testing.T) {
	t.Parallel()
	p := DefaultPolicy()
	p.Spot, p.GPU, p.OnDemand = MarketReserve{}, nil, MarketReserve{Warm: p.OnDemand.Warm}
	snapshot := func(hosts ...FleetHost) FleetSnapshot {
		s := planSnapshot(t, hosts...)
		s.Offers.Catalog = FleetCatalog()
		return s
	}
	busy := planHost(1, mustType(t, "c6a.2xlarge"), FleetServing)
	busy.Load, busy.Containers = cpuGiB(1000, 1), 1
	replacement := idle(planHost(2, mustType(t, "m7i.large"), FleetServing))
	replacement.Replaces = ptr(busy.ID)
	if plan := PlanFleet(p, snapshot(busy, replacement)); len(plan.Actions) > 0 {
		t.Fatalf("while the replaced host works: %+v", plan.Actions)
	}
	done := idle(planHost(1, mustType(t, "c6a.2xlarge"), FleetServing))
	plan := PlanFleet(p, snapshot(done, replacement))
	if got := hostsOf(plan.Actions); !slices.Equal(got, []HostID{{1}}) || plan.Actions[0].Kind != ActionDrain {
		t.Fatalf("once its work ends: %+v", plan.Actions)
	}
}

// A reserve resumed for a build goes back into the reserve: while it is the
// host that keeps the floor free, the reserve waits for it rather than
// buying its like. Within the keep window of its resume the market
// replaces nothing; once steady, rightsize replaces it with a cheaper type,
// and it returns as soon as the replacement serves.
func TestAResumedReserveReturnsOnceACheaperHostTakesItsSlot(t *testing.T) {
	t.Parallel()
	p := DefaultPolicy()
	p.Spot, p.GPU = MarketReserve{}, nil
	big := mustType(t, "c6a.8xlarge")
	p.OnDemand = MarketReserve{Warm: HeadroomTarget{Floor: cpuGiB(2000, 4)}, Stopped: HeadroomTarget{Floor: big.Usable(0)}}
	snapshot := func(readyFor time.Duration, hosts ...FleetHost) FleetSnapshot {
		resumed := planHost(1, big, FleetServing)
		resumed.PhaseAt, resumed.Slept = offerNow.Add(-readyFor), true
		s := planSnapshot(t, append([]FleetHost{resumed}, hosts...)...)
		s.Offers.Catalog = FleetCatalog()
		s.Holds = map[ReserveMarket]MarketHolds{onDemand: {Running: LoadPeak{Load: cpuGiB(2000, 4), At: offerNow.Add(-readyFor)}}}
		return s
	}
	plan := PlanFleet(p, snapshot(p.Keep/2))
	if mp := marketPlan(t, plan, onDemand); mp.Reason != WaitReturning || len(plan.Actions) > 0 {
		t.Fatalf("within the keep window: reason %q, actions %+v", mp.Reason, plan.Actions)
	}
	plan = PlanFleet(p, snapshot(p.Keep+time.Minute))
	moves := actionsOf(plan, ActionRightsize)
	if len(moves) != 1 || *moves[0].Host != (HostID{1}) || len(actionsOf(plan, ActionBuyReserve)) > 0 {
		t.Fatalf("steady: %+v", plan.Actions)
	}
	replacement := idle(planHost(2, moves[0].Offer.Type, FleetServing))
	plan = PlanFleet(p, snapshot(p.Keep+time.Minute, replacement))
	if got := actionsOf(plan, ActionReturnToReserve); len(got) != 1 || *got[0].Host != (HostID{1}) || len(actionsOf(plan, ActionBuyReserve)) > 0 {
		t.Fatalf("once the replacement serves: %+v", plan.Actions)
	}
}

// A reserve shortfall held for a host that may return names the hold, not
// an offer exclusion; with no host that could return it is bought at once.
func TestAHeldReserveShortfallNamesTheHold(t *testing.T) {
	t.Parallel()
	back := planHost(1, planSmall, FleetServing)
	back.PhaseAt, back.Load, back.Containers, back.Slept = offerNow, cpuGiB(1000, 1), 1, true
	plan := PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, back))
	if mp := marketPlan(t, plan, onDemand); mp.Reason != WaitReturning || len(actionsOf(plan, ActionBuyReserve)) > 0 {
		t.Fatalf("a host may return: reason %q, actions %+v", mp.Reason, plan.Actions)
	}
	plan = PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t))
	if mp := marketPlan(t, plan, onDemand); mp.Reason != WaitNone || len(actionsOf(plan, ActionBuyReserve)) != 1 {
		t.Fatalf("no host could return: reason %q, actions %+v", mp.Reason, plan.Actions)
	}
}

// Headroom no offer covers names what keeps the offers out: a cooldown,
// a vCPU quota, the purchase margin, or none at all.
func TestUncoveredHeadroomNamesTheExclusion(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		want    CapacityWait
		exclude func(*Policy, *OfferInputs)
	}{
		{WaitCooldown, func(_ *Policy, in *OfferInputs) {
			for _, typ := range in.Catalog {
				in.Cooldowns = append(in.Cooldowns, OfferCooldown{Region: "us-east-2", InstanceType: typ.Name, Market: MarketOnDemand, Until: offerNow.Add(time.Hour)})
			}
		}},
		{WaitQuota, func(_ *Policy, in *OfferInputs) {
			in.Quotas = []VCPUQuota{{Key: QuotaKey{Region: "us-east-2", Class: QuotaStandard, Market: MarketOnDemand}}}
		}},
		{WaitMargin, func(p *Policy, _ *OfferInputs) { p.MarginPercent = 100 }},
		{WaitNoOffer, func(_ *Policy, in *OfferInputs) { in.Networks = nil }},
	} {
		p := planPolicy(small, FleetCapacity{})
		s := planSnapshot(t)
		c.exclude(&p, &s.Offers)
		plan := PlanFleet(p, s)
		if mp := marketPlan(t, plan, onDemand); mp.Reason != c.want || len(plan.Actions) > 0 {
			t.Errorf("want %q: reason %q, actions %+v", c.want, mp.Reason, plan.Actions)
		}
	}
}

// While containers keep arriving a pass buys nothing and says how long the
// batch stays open, then buys once it closes. A container a stopped reserve
// fits resumes it, and one a starting host fits waits for that host, at
// once.
func TestPurchasesWaitForArrivalsToSettleButResumesDoNot(t *testing.T) {
	t.Parallel()
	need := Requirement{CPUMillis: 4000, MemoryBytes: 4 * gib}
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	batching := func(hosts ...FleetHost) (FleetSnapshot, uuid.UUID) {
		s := planSnapshot(t, hosts...)
		s.BatchWait = 800 * time.Millisecond
		g, id := pendingOne(need, nil)
		s.Pending = []DemandGroup{g}
		return s, id
	}

	s, id := batching()
	plan := PlanFleet(p, s)
	if w := waitOf(t, plan, id).Wait; len(plan.Actions) > 0 || plan.BatchWait != s.BatchWait || w == nil || *w != WaitBatch ||
		marketPlan(t, plan, onDemand).Reason != WaitBatch {
		t.Fatalf("while arriving: actions %+v, batch %s, wait %+v, reason %q", plan.Actions, plan.BatchWait, waitOf(t, plan, id),
			marketPlan(t, plan, onDemand).Reason)
	}
	s.BatchWait = 0
	if plan := PlanFleet(p, s); len(actionsOf(plan, ActionBuy)) != 1 || plan.BatchWait != 0 {
		t.Fatalf("once settled: actions %+v, batch %s", plan.Actions, plan.BatchWait)
	}

	s, id = batching(planHost(1, planSmall, FleetStopped))
	if plan := PlanFleet(p, s); len(actionsOf(plan, ActionResume)) != 1 || len(plan.Actions) != 1 || plan.BatchWait != 0 ||
		*waitOf(t, plan, id).Host != (HostID{1}) {
		t.Fatalf("a fitting reserve: actions %+v, batch %s", plan.Actions, plan.BatchWait)
	}

	s, id = batching(planHost(1, planSmall, FleetStarting))
	if plan := PlanFleet(p, s); len(plan.Actions) > 0 || plan.BatchWait != 0 || *waitOf(t, plan, id).Host != (HostID{1}) {
		t.Fatalf("a starting host: actions %+v, batch %s, wait %+v", plan.Actions, plan.BatchWait, waitOf(t, plan, id))
	}
}

// gpuPlanSnapshot is the plan tests' zone with the fleet catalog, L4 and
// A10G Spot quotes and the hosts.
func gpuPlanSnapshot(t *testing.T, hosts ...FleetHost) FleetSnapshot {
	s := planSnapshot(t, hosts...)
	s.Offers.Catalog = FleetCatalog()
	for typ, micros := range map[string]int64{"g6.xlarge": 300_000, "g6.4xlarge": 500_000, "g5.2xlarge": 500_000} {
		s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: typ, HourlyMicros: micros, ObservedAt: offerNow})
	}
	return s
}

// gpuReserve is a stopped g6.xlarge L4 reserve in market.
func gpuReserve(t *testing.T, id byte, market Market) FleetHost {
	h := planHost(id, mustType(t, "g6.xlarge"), FleetStopped)
	h.Market, h.GPU = market, "L4"
	return h
}

// spotCooled cools every Spot pool of model's types in the plan zone.
func spotCooled(model string) []OfferCooldown {
	var out []OfferCooldown
	for _, typ := range FleetCatalog() {
		if typ.GPU == model {
			out = append(out, OfferCooldown{Region: "us-east-2", InstanceType: typ.Name, Market: MarketSpot, Until: offerNow.Add(time.Hour)})
		}
	}
	return out
}

func gpuPolicy() Policy {
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.GPU = DefaultPolicy().GPU
	return p
}

// GPU work resumes a reserve of its own market: Spot-tolerant work the
// Spot reserve, work that cannot run on Spot the on-demand one.
func TestGPUWorkResumesAReserveOfItsMarket(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		preemptible bool
		want        HostID
	}{{true, HostID{1}}, {false, HostID{2}}} {
		s := gpuPlanSnapshot(t, gpuReserve(t, 1, MarketSpot), gpuReserve(t, 2, MarketOnDemand))
		s.Pending = []DemandGroup{{Need: Requirement{Preemptible: c.preemptible, GPUs: []string{"L4"}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 4 * gib},
			Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}}}}
		plan := PlanFleet(gpuPolicy(), s)
		if got := hostsOf(actionsOf(plan, ActionResume)); !slices.Equal(got, []HostID{c.want}) || len(boughtForWork(plan)) > 0 {
			t.Errorf("preemptible %t: resumed %v, bought %v", c.preemptible, got, boughtForWork(plan))
		}
	}
}

// Spot-tolerant work for any card borrows the on-demand reserve once the
// Spot pools of every reserved card refuse it: a Spot offer of a card it
// would not buy, as L40S, keeps none of it on Spot.
func TestAnyCardSpotWorkBorrowsWhileEveryReservedCardsSpotRefuses(t *testing.T) {
	t.Parallel()
	s := gpuPlanSnapshot(t, gpuReserve(t, 2, MarketOnDemand))
	s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: "g6e.xlarge", HourlyMicros: 300_000, ObservedAt: offerNow})
	for _, model := range []string{"T4", "A10G", "L4"} {
		s.Offers.Cooldowns = append(s.Offers.Cooldowns, spotCooled(model)...)
	}
	g, _ := pendingOne(Requirement{Preemptible: true, GPUs: []string{GPUAny}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 4 * gib}, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(gpuPolicy(), s)
	if resumed := hostsOf(actionsOf(plan, ActionResume)); !slices.Equal(resumed, []HostID{{2}}) {
		t.Fatalf("resumed %v, bought %v, want the on-demand L4 reserve", resumed, boughtForWork(plan))
	}
}

// Spot-tolerant GPU work buys Spot of each model it accepts before
// on-demand of any: with its first model's Spot pools refusing it buys
// its next model on Spot, and only with every model's refusing on-demand.
func TestSpotGPUWorkBuysOnDemandOnlyWhenNoModelsSpotCanHostIt(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		cooled []string
		want   string
	}{{[]string{"L4"}, "g5.2xlarge/spot"}, {[]string{"L4", "A10G"}, "g6.xlarge/on_demand"}} {
		s := gpuPlanSnapshot(t)
		s.Offers.OwnerPays = true
		for _, model := range c.cooled {
			s.Offers.Cooldowns = append(s.Offers.Cooldowns, spotCooled(model)...)
		}
		g, _ := pendingOne(Requirement{Preemptible: true, GPUs: []string{"L4", "A10G"}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 4 * gib}, nil)
		s.Pending = []DemandGroup{g}
		var got []string
		for _, a := range actionsOf(PlanFleet(gpuPolicy(), s), ActionBuy) {
			if len(a.Containers) > 0 {
				got = append(got, a.Offer.Type.Name+"/"+string(a.Offer.Market))
			}
		}
		if !slices.Equal(got, []string{c.want}) {
			t.Errorf("Spot of %v cooling: bought %v, want %s", c.cooled, got, c.want)
		}
	}
}

// A Spot GPU market whose reserve serves waits for it to return rather than
// buy another, however long it has served; the CPU Spot market buys once
// its host has served past the keep window.
func TestASpotGPUReserveThatServesIsAwaitedNotReplaced(t *testing.T) {
	t.Parallel()
	for _, gpu := range []bool{true, false} {
		h := planHost(1, mustType(t, "c6a.4xlarge"), FleetServing)
		m := ReserveMarket{Preemptible: true}
		if gpu {
			h = planHost(1, mustType(t, "g6.4xlarge"), FleetServing)
			h.GPU, m.GPU = "L4", "L4"
		}
		h.Market, h.Load, h.Containers, h.PhaseAt = MarketSpot, FleetCapacity{CPUMillis: 1000, MemoryBytes: gib, GPUs: h.Usable.GPUs}, 1, offerNow.Add(-time.Hour)
		s := gpuPlanSnapshot(t, h)
		s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: "c6a.4xlarge", HourlyMicros: 200_000, ObservedAt: offerNow})
		p := gpuPolicy()
		p.Spot = DefaultPolicy().Spot
		var bought []FleetAction
		for _, a := range actionsOf(PlanFleet(p, s), ActionBuyReserve) {
			if a.Market == m {
				bought = append(bought, a)
			}
		}
		if (len(bought) == 0) != gpu {
			t.Errorf("%s bought reserves %+v while its host serves", m, bought)
		}
	}
}

// An offer cools only when the host bought from it is too small ever to
// take its container: a full host whose container a starting host covers
// cools nothing, a host of a type smaller than its container does.
func TestPlanCoolsOnlyOffersTooSmallForTheirContainer(t *testing.T) {
	t.Parallel()
	full := planHost(1, planLarge, FleetServing)
	full.Load, full.Containers = full.Usable, 1
	coming := planHost(2, planLarge, FleetStarting)
	tiny := planHost(3, planSmall, FleetServing)
	s := planSnapshot(t, full, coming, tiny)
	s.Pending = []DemandGroup{
		{Need: Requirement{CPUMillis: 4000, MemoryBytes: 8 * gib}, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow, Host: &full.ID}}},
		{Need: Requirement{CPUMillis: 16000, MemoryBytes: 8 * gib}, Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow, Host: &tiny.ID}}},
	}
	_, cools := planOwner(planPolicy(FleetCapacity{}, FleetCapacity{}), s, time.Minute)
	if len(cools) != 1 || cools[0].InstanceType != planSmall.Name {
		t.Fatalf("cooled %+v, want only the offer of the host too small for its container", cools)
	}
}

// A smaller size whose pools stop cooling within the batch window still
// holds the size rule's step: with every g6.xlarge pool cooling for two
// more seconds, the L4 job buys no g6.2xlarge or g6.4xlarge, and buys the
// g6.xlarge once it frees.
func TestASizeCoolingForSecondsHoldsTheStepUp(t *testing.T) {
	t.Parallel()
	in := offerInputs(t)
	for _, typ := range []string{"g6.xlarge", "g6.2xlarge", "g6.4xlarge"} {
		price, _ := mustType(t, typ).OnDemandMicros("us-east-2")
		in.Spot = append(in.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: typ, HourlyMicros: price * 4 / 10, ObservedAt: offerNow})
	}
	in.Cooldowns = []OfferCooldown{{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: "g6.xlarge", Market: MarketSpot, Until: offerNow.Add(2 * time.Second)}}
	s := FleetSnapshot{Now: offerNow, Offers: in, HostRoom: 10, ReserveRoom: 10,
		Pending: []DemandGroup{{Need: Requirement{Preemptible: true, GPUs: []string{"L4"}, CPUMillis: 2000, MemoryBytes: 13056 << 20},
			Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow}}}}}
	if bought := boughtForWork(PlanFleet(demandOnly(), s)); len(bought) > 0 {
		t.Fatalf("bought %v while g6.xlarge cools for two seconds", bought)
	}
	s.Now, s.Offers.Now = offerNow.Add(3*time.Second), offerNow.Add(3*time.Second)
	if bought := boughtForWork(PlanFleet(demandOnly(), s)); !slices.Equal(bought, []string{"g6.xlarge"}) {
		t.Fatalf("bought %v once g6.xlarge freed, want a g6.xlarge", bought)
	}
}

// demandOnly is the policy without headroom or reserves: the fleet buys
// only for pending work.
func demandOnly() Policy {
	p := DefaultPolicy()
	p.Spot, p.OnDemand, p.GPU = MarketReserve{}, MarketReserve{}, nil
	return p
}

// l4Job is the Spot-tolerant L4 function prod ran on 2026-10-13.
var l4Job = Requirement{Preemptible: true, GPUs: []string{"L4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 13056 << 20}

// spotQuoted adds a Spot quote of each type in the plan zone.
func spotQuoted(s FleetSnapshot, micros int64, types ...string) FleetSnapshot {
	for _, typ := range types {
		s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: typ, HourlyMicros: micros, ObservedAt: offerNow})
	}
	return s
}

// cooling cools the Spot pools of each type in the plan region for an hour.
func cooling(types ...string) []OfferCooldown {
	var out []OfferCooldown
	for _, typ := range types {
		out = append(out, OfferCooldown{Region: "us-east-2", InstanceType: typ, Market: MarketSpot, Until: offerNow.Add(time.Hour)})
	}
	return out
}

// An L4 job's purchase keeps the size rule while L4 Spot pools refuse, as
// every one did on 2026-10-13: it steps up one size while the smallest
// cools, though a larger one quotes less, and never past the size cap or
// to a type of more cards, however many smaller sizes cool. One job keeps
// no warm card beside it.
func TestAnL4JobBuysTheSmallestSizeItsPoolsAllow(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		cooled []string
		want   []string
	}{
		{[]string{"g6.xlarge"}, []string{"g6.2xlarge/spot"}},
		{[]string{"g6.xlarge", "g6.2xlarge", "g6.4xlarge"}, []string{"g6.xlarge/on_demand"}},
	} {
		s := planSnapshot(t)
		s.Offers.Catalog, s.Offers.OwnerPays = FleetCatalog(), true
		s = spotQuoted(spotQuoted(s, 300_000, "g6.xlarge", "g6.2xlarge", "g6.8xlarge", "g6.12xlarge", "g6.16xlarge", "g6.48xlarge"), 250_000, "g6.4xlarge")
		s.Offers.Cooldowns = cooling(c.cooled...)
		g, _ := pendingOne(l4Job, nil)
		s.Pending = []DemandGroup{g}
		var got []string
		for _, a := range actionsOf(PlanFleet(gpuPolicy(), s), ActionBuy) {
			got = append(got, a.Offer.Type.Name+"/"+string(a.Offer.Market))
		}
		if !slices.Equal(got, c.want) {
			t.Errorf("%v cooling: bought %v, want %v", c.cooled, got, c.want)
		}
	}
}

// A GPU market keeps its warm share in whole cards: four running L4 jobs
// keep one idle card beside them, one job keeps none, so a lone GPU job
// never holds a second GPU host (prod held one beside every on-demand GPU
// job on 2026-10-15). The card is bought within the size rule.
func TestGPUWarmShareIsWholeCards(t *testing.T) {
	t.Parallel()
	typ := mustType(t, "g6.xlarge")
	busy := func(id byte) FleetHost {
		h := planHost(id, typ, FleetServing)
		h.Market, h.GPU, h.Containers = MarketSpot, "L4", 1
		h.Load = FleetCapacity{CPUMillis: 1000, MemoryBytes: 8 * gib, GPUs: 1}
		return h
	}
	snapshot := func(hosts ...FleetHost) FleetSnapshot {
		s := spotQuoted(planSnapshot(t, hosts...), 300_000, "g6.xlarge", "g6.2xlarge")
		s.Offers.Catalog = FleetCatalog()
		return s
	}
	if got := actionsOf(PlanFleet(gpuPolicy(), snapshot(busy(1))), ActionBuy); len(got) > 0 {
		t.Fatalf("one job: bought %+v, want no warm card", got)
	}
	got := actionsOf(PlanFleet(gpuPolicy(), snapshot(busy(1), busy(2), busy(3), busy(4))), ActionBuy)
	if len(got) != 1 || got[0].Offer.Type.Name != "g6.xlarge" || got[0].Offer.Market != MarketSpot {
		t.Fatalf("four jobs: bought %+v, want one Spot g6.xlarge warm card", got)
	}
}

// No GPU reserve is bought above the size cap while the pools within it
// cool, as every GPU Spot pool refused on 2026-10-12: the Spot L4 market
// waits for them, though larger types quote far less.
func TestAGPUReserveStaysWithinTheCapWhileSmallerPoolsCool(t *testing.T) {
	t.Parallel()
	spotL4 := ReserveMarket{Preemptible: true, GPU: "L4"}
	s := spotQuoted(spotQuoted(planSnapshot(t), 300_000, "g6.xlarge", "g6.2xlarge", "g6.4xlarge"), 50_000, "g6.8xlarge", "g6.12xlarge", "g6.16xlarge", "g6.48xlarge")
	s.Offers.Catalog = FleetCatalog()
	s.Offers.Cooldowns = cooling("g6.xlarge", "g6.2xlarge", "g6.4xlarge")
	plan := PlanFleet(gpuPolicy(), s)
	for _, a := range actionsOf(plan, ActionBuyReserve) {
		if a.Market == spotL4 {
			t.Errorf("bought reserve %s", a.Offer.Key())
		}
	}
	if mp := marketPlan(t, plan, spotL4); mp.Reason != WaitCooldown {
		t.Errorf("the Spot L4 market's reason %q, want %q", mp.Reason, WaitCooldown)
	}
}

// Work resumes a reserve that cannot serve at once only when it serves
// sooner than a purchase. EC2 starts a stopped Spot instance only once it
// disables the persistent request, 38-165 s after the stop (2026-10-13):
// L4 work waits for a Spot L4 reserve that stopped 33 s ago, since the
// rest of that wait and a GPU resume beat a GPU launch, while CPU work
// buys past a CPU one; past SpotDisableMax the L4 work stops waiting and
// buys. A reserve stopped on an older agent build updates it first, which
// a GPU launch still outlasts.
func TestWorkResumesAReserveOnlyWhenItServesSoonerThanAPurchase(t *testing.T) {
	t.Parallel()
	p := gpuPolicy()
	cpuSpot := Requirement{Preemptible: true, CPUMillis: 4000, MemoryBytes: 8 * gib}
	for _, c := range []struct {
		name    string
		gpu     bool
		state   FleetState
		stopped time.Duration
		resumes bool
	}{
		{"L4, request active", true, FleetStopping, 33 * time.Second, true},
		{"CPU, request active", false, FleetStopping, 33 * time.Second, false},
		{"L4, request active past the bound", true, FleetStopping, p.Starts.SpotDisableMax + time.Second, false},
	} {
		h := planHost(1, planSmall, c.state)
		need := cpuSpot
		if c.gpu {
			h = gpuReserve(t, 1, MarketSpot)
			h.State, need = c.state, l4Job
		}
		h.Market, h.StoppedAt = MarketSpot, ptr(offerNow.Add(-c.stopped))
		s := spotQuoted(gpuPlanSnapshot(t, h), 200_000, "m.small")
		s.Offers.Catalog = append(s.Offers.Catalog, planSmall)
		g, _ := pendingOne(need, nil)
		s.Pending = []DemandGroup{g}
		plan := PlanFleet(p, s)
		resumed := slices.Equal(hostsOf(actionsOf(plan, ActionResume)), []HostID{h.ID})
		if resumed != c.resumes || resumed == (len(boughtForWork(plan)) == 1) {
			t.Errorf("%s: resumed %t, bought %v; want a resume %t", c.name, resumed, boughtForWork(plan), c.resumes)
		}
	}
}

// GPU capacity bought for work is kept: once an L4 job's Spot host, on a
// persistent request, idles, it stops into the reserve its market lacks
// instead of the market buying one.
func TestAnIdleGPUWorkHostBecomesItsMarketsMissingReserve(t *testing.T) {
	t.Parallel()
	h := idle(planHost(1, mustType(t, "g6.4xlarge"), FleetServing))
	h.Market, h.GPU = MarketSpot, "L4"
	plan := PlanFleet(gpuPolicy(), gpuPlanSnapshot(t, h))
	var bought []string
	for _, a := range actionsOf(plan, ActionBuyReserve) {
		if a.Market.GPU == "L4" {
			bought = append(bought, a.Offer.Key())
		}
	}
	if got := hostsOf(actionsOf(plan, ActionReturnToReserve)); !slices.Equal(got, []HostID{h.ID}) || len(bought) > 0 {
		t.Fatalf("returned %v and bought L4 reserves %v, want the L4 host returned and none bought", got, bought)
	}
}

// Recurring bursts keep their reserves: within the cost horizon of the
// market's peak load, the reserves a burst needed stay stopped and a host
// woken from the reserve for it goes back, so the next burst resumes them
// rather than the market retiring and buying them again. Past the horizon
// they retire and the host drains.
func TestReservesForALoadPeakStayForTheCostHorizon(t *testing.T) {
	t.Parallel()
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.OnDemand.Stopped.LoadPercent = 50
	woken := idle(planHost(3, planSmall, FleetServing))
	woken.Slept = true
	for _, c := range []struct {
		ago   time.Duration
		keeps bool
	}{{10 * time.Minute, true}, {p.CostHorizon, false}} {
		s := planSnapshot(t, planHost(1, planSmall, FleetStopped), planHost(2, planSmall, FleetStopped), woken)
		s.Holds = map[ReserveMarket]MarketHolds{onDemand: {Stopped: LoadPeak{Load: small.Times(3), At: offerNow.Add(-c.ago)}}}
		plan := PlanFleet(p, s)
		retired, returned := actionsOf(plan, ActionRetireReserve), actionsOf(plan, ActionReturnToReserve)
		if c.keeps && (len(retired) > 0 || len(returned) != 1) || !c.keeps && (len(retired) != 2 || len(actionsOf(plan, ActionDrain)) != 1) {
			t.Errorf("peak %s ago: actions %+v", c.ago, plan.Actions)
		}
	}
}

// Spot headroom that follows the arrival rate is bought with the work it
// arrives beside rather than after it, so a ramp's next arrivals find
// room instead of waiting a launch: eight pending 1-CPU containers and a
// steady 12 CPU of arrivals buy hosts that hold the work and the 13 CPU
// warm target at once.
func TestSpotHeadroomIsBoughtWithTheWork(t *testing.T) {
	t.Parallel()
	spot := ReserveMarket{Preemptible: true}
	p := planPolicy(FleetCapacity{}, FleetCapacity{})
	p.Spot = MarketReserve{Warm: DefaultPolicy().Spot.Warm}
	s := spotQuoted(planSnapshot(t), 100_000, "c6a.xlarge", "c6a.2xlarge", "c6a.4xlarge", "c6a.8xlarge")
	s.Offers.Catalog = FleetCatalog()
	s.Arrived = map[ReserveMarket]FleetCapacity{spot: cpuGiB(12_000, 24)}
	work := DemandGroup{Need: Requirement{Preemptible: true, CPUMillis: 1000, MemoryBytes: 2 * gib}}
	for range 8 {
		work.Containers = append(work.Containers, PendingContainer{ID: uuid.New(), Since: offerNow})
	}
	s.Pending = []DemandGroup{work}
	plan := PlanFleet(p, s)
	var bought FleetCapacity
	for _, a := range actionsOf(plan, ActionBuy) {
		bought = bought.Plus(a.Offer.Usable)
	}
	mp := marketPlan(t, plan, spot)
	if !mp.Shortfall.Empty() || !bought.Covers(cpuGiB(8000+mp.WarmTarget.CPUMillis, 16)) {
		t.Fatalf("bought hosts of %+v, shortfall %+v (%s); want the 8 CPU of work and the %v warm target", bought, mp.Shortfall, mp.Reason, mp.WarmTarget.CPUMillis)
	}
}

// CPU work that packs takes the spare room of a fleet GPU host running GPU
// work, which the GPU work already pays for, instead of buying a CPU host;
// work that cannot pack buys one.
func TestPackedCPUWorkBuysNoHostWhileAGPUHostHasRoom(t *testing.T) {
	t.Parallel()
	h := planHost(1, mustType(t, "g6.4xlarge"), FleetServing)
	h.Market, h.GPU = MarketSpot, "L4"
	h.Load, h.Containers = FleetCapacity{CPUMillis: 2000, MemoryBytes: 13 * gib, GPUs: 1}, 1
	for _, packs := range []bool{true, false} {
		s := spotQuoted(gpuPlanSnapshot(t, h), 100_000, "c6a.xlarge", "c6a.2xlarge")
		g, id := pendingOne(Requirement{Preemptible: true, Packs: packs, CPUMillis: 4000, MemoryBytes: 8 * gib}, nil)
		s.Pending = []DemandGroup{g}
		plan := PlanFleet(gpuPolicy(), s)
		if bought := boughtForWork(plan); (len(bought) == 0) != packs || packs && waitOf(t, plan, id).Wait != nil {
			t.Errorf("packs %t: bought %v, wait %+v", packs, bought, waitOf(t, plan, id))
		}
	}
}

// A pool cooling after refusals costs what its absence would: the pass
// buys exactly what it buys were that pool never quoted.
func TestARefusedPoolBuysWhatItsAbsenceWould(t *testing.T) {
	t.Parallel()
	g, _ := pendingOne(Requirement{Preemptible: true, CPUMillis: 4000, MemoryBytes: 8 * gib}, nil)
	snapshot := func(cheap bool) FleetSnapshot {
		s := planSnapshot(t)
		s.Offers.Catalog, s.Pending = FleetCatalog(), []DemandGroup{g}
		s = spotQuoted(s, 140_000, "m7i.2xlarge", "c6a.4xlarge")
		if cheap {
			s = spotQuoted(s, 90_000, "c6a.2xlarge")
		}
		return s
	}
	refused := snapshot(true)
	refused.Offers.Cooldowns = cooling("c6a.2xlarge")
	got, want := PlanFleet(demandOnly(), refused), PlanFleet(demandOnly(), snapshot(false))
	if !slices.Equal(boughtForWork(got), boughtForWork(want)) || len(boughtForWork(got)) != 1 {
		t.Fatalf("with c6a.2xlarge refused bought %v, without it %v", boughtForWork(got), boughtForWork(want))
	}
	if cheap := boughtForWork(PlanFleet(demandOnly(), snapshot(true))); !slices.Equal(cheap, []string{"c6a.2xlarge"}) {
		t.Fatalf("with c6a.2xlarge quoted and not cooling bought %v", cheap)
	}
}

// Spot-tolerant GPU work no Spot offer can host buys an on-demand host at a
// loss only once it has waited the Spot wait; until then it waits on the
// cooling Spot pools.
func TestSpotGPUWorkBuysOnDemandOnceItHasWaitedTheSpotWait(t *testing.T) {
	p := gpuPolicy()
	for _, c := range []struct {
		waited time.Duration
		buys   bool
	}{{0, false}, {p.SpotWait, true}} {
		s := gpuPlanSnapshot(t)
		s.Offers.Cooldowns = slices.Concat(spotCooled("L4"), spotCooled("A10G"))
		id := uuid.New()
		s.Pending = []DemandGroup{{Need: Requirement{Preemptible: true, GPUs: []string{"L4", "A10G"}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 4 * gib},
			Containers: []PendingContainer{{ID: id, Since: offerNow.Add(-c.waited)}}}}
		plan := PlanFleet(p, s)
		bought := boughtForWork(plan)
		if (len(bought) == 1) != c.buys {
			t.Errorf("after %s: bought %v, want a purchase %t", c.waited, bought, c.buys)
		}
		for _, a := range actionsOf(plan, ActionBuy) {
			if a.Reason != ReasonSpotWait {
				t.Errorf("after %s: bought %s for %s, want the Spot wait, which its launch walk keeps", c.waited, a.Offer.Type.Name, a.Reason)
			}
		}
		if w := waitOf(t, plan, id); !c.buys && (w.Wait == nil || *w.Wait != WaitCooldown) {
			t.Errorf("after %s: wait %+v, want the cooling Spot pools", c.waited, w)
		}
	}
}

// Spot-tolerant GPU work whose Spot offers only fail the margin buys Spot
// at a loss once it has waited the Spot wait, where Spot costs less than
// on-demand.
func TestSpotGPUWorkAtALossBuysSpotFirst(t *testing.T) {
	s := gpuPlanSnapshot(t)
	s.Offers.Spot = nil
	for _, typ := range FleetCatalog() {
		if typ.GPU == "L4" || typ.GPU == "A10G" {
			price, _ := typ.OnDemandMicros("us-east-2")
			s.Offers.Spot = append(s.Offers.Spot, SpotQuote{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: typ.Name, HourlyMicros: price * 95 / 100, ObservedAt: offerNow})
		}
	}
	s.Pending = []DemandGroup{{Need: Requirement{Preemptible: true, GPUs: []string{"L4", "A10G"}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 4 * gib},
		Containers: []PendingContainer{{ID: uuid.New(), Since: offerNow.Add(-gpuPolicy().SpotWait)}}}}
	var markets []Market
	for _, a := range actionsOf(PlanFleet(gpuPolicy(), s), ActionBuy) {
		if len(a.Containers) > 0 {
			markets = append(markets, a.Offer.Market)
		}
	}
	if !slices.Equal(markets, []Market{MarketSpot}) {
		t.Fatalf("bought %v for the work, want one Spot host", markets)
	}
}

// A deploy that marks the host holding the warm floor due an agent update
// buys nothing: the host holds the floor while it updates, and only
// pending work, which placement gives it no room for, buys past it.
func TestADeployBuysNothingForAHostUpdatingItsAgent(t *testing.T) {
	h := planHost(1, planSmall, FleetServing)
	h.UpdateDue = true
	if plan := PlanFleet(planPolicy(small, FleetCapacity{}), planSnapshot(t, h)); len(plan.Actions) > 0 {
		t.Fatalf("actions %+v after a deploy", plan.Actions)
	}
}

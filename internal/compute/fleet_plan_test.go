package compute

import (
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

// The plan tests use two shapes like the reference's: small (8 vCPU,
// 16 GiB) and large (32 vCPU, 64 GiB, four smalls' usable capacity).
var (
	planSmall = CatalogType{Name: "small", CPUMillis: 8000, MemoryBytes: 16 * gib, prices: [4]int64{100_000, 100_000, 100_000, 100_000}}
	planLarge = CatalogType{Name: "large", CPUMillis: 32_000, MemoryBytes: 64 * gib, prices: [4]int64{300_000, 300_000, 300_000, 300_000}}
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
		Market: MarketOnDemand, Usable: typ.Usable(0), State: state, Current: true,
		HourlyMicros: ptr(price + rootDiskMicros("us-east-2", rootVolumeGiB) + 5000),
	}
	if h.reserve() {
		h.ReserveMode = ptr(ReserveStop)
	}
	return h
}

// idle makes a serving host lightly used for an hour.
func idle(h FleetHost) FleetHost {
	h.LightSince = ptr(offerNow.Add(-time.Hour))
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

func boughtTypes(plan FleetPlan, kind FleetActionKind) []string {
	var names []string
	for _, a := range actionsOf(plan, kind) {
		names = append(names, a.Offer.Type.Name)
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
	for _, c := range []struct {
		target FleetCapacity
		want   []string
	}{
		{small, []string{"small"}},
		{small.Times(4), []string{"large"}},
		{large.Times(4), []string{"large", "large", "large", "large"}},
	} {
		plan := PlanFleet(planPolicy(c.target, FleetCapacity{}), planSnapshot(t))
		if got := boughtTypes(plan, ActionBuy); !slices.Equal(got, c.want) {
			t.Errorf("target %+v bought %v", c.target, got)
		}
		if mp := marketPlan(t, plan, onDemand); !mp.Shortfall.Empty() {
			t.Errorf("target %+v left %+v short", c.target, mp.Shortfall)
		}
	}
}

func TestPlanFitsALargeRecentShapeOnOneHostDespiteAggregateRoom(t *testing.T) {
	s := planSnapshot(t, planHost(1, planSmall, FleetServing), planHost(2, planSmall, FleetServing),
		planHost(3, planSmall, FleetServing), planHost(4, planSmall, FleetServing))
	s.Forecasts = map[ReserveMarket]MarketForecast{onDemand: {Shapes: []FleetCapacity{cpuGiB(16_000, 32)}}}
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), s)
	if got := boughtTypes(plan, ActionBuy); !slices.Equal(got, []string{"large"}) {
		t.Fatalf("bought %v", got)
	}
}

func TestPlanResumesOnlyAReserveReadyForTheCurrentAgent(t *testing.T) {
	for _, current := range []bool{true, false} {
		reserve := planHost(1, planSmall, FleetStopped)
		reserve.Current = current
		plan := PlanFleet(planPolicy(small, FleetCapacity{}), planSnapshot(t, reserve))
		resumed, bought := len(actionsOf(plan, ActionResume)) > 0, len(actionsOf(plan, ActionBuy)) > 0
		if resumed != current || bought == current {
			t.Errorf("current %v: resumed %v bought %v", current, resumed, bought)
		}
	}
}

func TestPlanRefreshesAStaleReserveTheTargetNeedsAndRetiresOneItDoesNot(t *testing.T) {
	stale := planHost(1, planSmall, FleetStopped)
	stale.Current = false
	needed := PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, stale))
	if got := hostsOf(needed.Actions); len(needed.Actions) != 1 || needed.Actions[0].Kind != ActionRefresh || got[0] != stale.ID {
		t.Fatalf("needed: %+v", needed.Actions)
	}
	surplus := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), planSnapshot(t, stale))
	if len(surplus.Actions) != 1 || surplus.Actions[0].Kind != ActionRetireReserve {
		t.Fatalf("surplus: %+v", surplus.Actions)
	}
}

func TestPlanKeepsTheOnlyHostThatFitsRecentRequests(t *testing.T) {
	hosts := []FleetHost{idle(planHost(9, planLarge, FleetServing))}
	for i := range 4 {
		hosts = append(hosts, idle(planHost(byte(i+1), planSmall, FleetServing)))
	}
	s := planSnapshot(t, hosts...)
	s.Forecasts = map[ReserveMarket]MarketForecast{onDemand: {Shapes: []FleetCapacity{large}}}
	plan := PlanFleet(planPolicy(small.Times(4), FleetCapacity{}), s)
	if drained := hostsOf(actionsOf(plan, ActionDrain)); slices.Contains(drained, HostID{9}) || len(drained) != 4 {
		t.Fatalf("drained %v", drained)
	}
}

func TestPlanReportsARecentShapeNoOfferFits(t *testing.T) {
	s := planSnapshot(t, planHost(1, planSmall, FleetServing))
	s.Forecasts = map[ReserveMarket]MarketForecast{onDemand: {Shapes: []FleetCapacity{large.Times(2)}}}
	mp := marketPlan(t, PlanFleet(planPolicy(small, FleetCapacity{}), s), onDemand)
	if !slices.Equal(mp.UnmetShapes, []FleetCapacity{large.Times(2)}) || mp.Reason != ReasonNoOffer {
		t.Fatalf("unmet %v reason %q", mp.UnmetShapes, mp.Reason)
	}
}

func TestPlanKeepsElectiveGrowthOutOfDemandAndRecovery(t *testing.T) {
	demand := planSnapshot(t)
	demand.Pending = []DemandGroup{{Need: Requirement{CPUMillis: 1000, MemoryBytes: gib}, Containers: []PendingContainer{{ID: uuid.New()}}}}
	recovering := planSnapshot(t, func() FleetHost {
		h := planHost(1, planLarge, FleetDraining)
		h.Protected = true
		return h
	}())
	for name, s := range map[string]FleetSnapshot{"demand": demand, "recovery": recovering} {
		plan := PlanFleet(planPolicy(large, FleetCapacity{}), s)
		for _, a := range plan.Actions {
			if len(a.Containers) == 0 {
				t.Errorf("%s: elective %s", name, a.Kind)
			}
		}
		if mp := marketPlan(t, plan, onDemand); mp.Reason != ReasonDemandOrRecovery {
			t.Errorf("%s: reason %q", name, mp.Reason)
		}
	}
}

func TestPlanPendingCapacityNeverJustifiesRetiringReadyCapacity(t *testing.T) {
	s := planSnapshot(t, idle(planHost(1, planSmall, FleetServing)), planHost(2, planLarge, FleetStarting))
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), s)
	mp := marketPlan(t, plan, onDemand)
	if len(actionsOf(plan, ActionDrain, ActionReturnToReserve)) > 0 || mp.WarmFree != small || mp.WarmPending != large {
		t.Fatalf("actions %+v free %+v pending %+v", plan.Actions, mp.WarmFree, mp.WarmPending)
	}
}

func TestPlanConsolidatesOnlyMovableWorkAndKeepsTheWarmTarget(t *testing.T) {
	for _, c := range []struct {
		pinned int
		target FleetCapacity
		moves  bool
	}{
		{0, small, true},
		{1, small, false},
		{0, large.Times(2), false},
	} {
		busy := idle(planHost(1, planLarge, FleetServing))
		busy.Load, busy.Containers, busy.Pinned = cpuGiB(1000, 2), 1, c.pinned
		s := planSnapshot(t, busy, planHost(2, planLarge, FleetServing))
		plan := PlanFleet(planPolicy(c.target, FleetCapacity{}), s)
		moved := actionsOf(plan, ActionConsolidate)
		if (len(moved) == 1) != c.moves || (c.moves && (*moved[0].Host != HostID{1} || *moved[0].Destination != HostID{2})) {
			t.Errorf("pinned %d target %+v: %+v", c.pinned, c.target, moved)
		}
	}
}

func TestPlanConsolidationKeepsItsDestination(t *testing.T) {
	busy := idle(planHost(1, planLarge, FleetServing))
	busy.Load, busy.Containers = cpuGiB(1000, 2), 1
	destination := idle(planHost(2, planLarge, FleetServing))
	destination.HourlyMicros = ptr(int64(200_000))
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), planSnapshot(t, busy, destination))
	if moved := actionsOf(plan, ActionConsolidate); len(moved) != 1 || *moved[0].Destination != (HostID{2}) {
		t.Fatalf("consolidation %+v", moved)
	}
	if left := actionsOf(plan, ActionDrain, ActionReturnToReserve); len(left) > 0 {
		t.Fatalf("the destination left: %+v", left)
	}
}

func TestPlanConsolidatesOneHostAtATimeAndWaitsOutTheCooldown(t *testing.T) {
	busy := idle(planHost(1, planLarge, FleetServing))
	busy.Load, busy.Containers = cpuGiB(1000, 2), 1
	s := planSnapshot(t, busy, planHost(2, planLarge, FleetServing))
	for name, r := range map[string]MarketRecord{
		"moving":  {ConsolidatingHost: ptr(HostID{3}), ConsolidationStarted: ptr(offerNow.Add(-time.Minute))},
		"cooling": {CooldownUntil: ptr(offerNow.Add(time.Minute))},
	} {
		s.Markets = map[ReserveMarket]MarketRecord{onDemand: r}
		if moved := actionsOf(PlanFleet(planPolicy(small, FleetCapacity{}), s), ActionConsolidate); len(moved) > 0 {
			t.Errorf("%s: consolidated %+v", name, moved)
		}
	}
	s.Markets = map[ReserveMarket]MarketRecord{onDemand: {ConsolidatingHost: ptr(HostID{3}), ConsolidationStarted: ptr(offerNow.Add(-2 * time.Hour))}}
	if moved := actionsOf(PlanFleet(planPolicy(small, FleetCapacity{}), s), ActionConsolidate); len(moved) != 1 {
		t.Fatalf("a consolidation past its deadline still blocks the next")
	}
}

func TestPlanRightsizesAnIdleHostAndKeepsItUntilTheReplacementServes(t *testing.T) {
	source := idle(planHost(1, planLarge, FleetServing))
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), planSnapshot(t, source))
	moves := actionsOf(plan, ActionRightsize)
	if len(moves) != 1 || *moves[0].Host != (HostID{1}) || moves[0].Offer.Type.Name != "small" || len(actionsOf(plan, ActionDrain)) > 0 {
		t.Fatalf("actions %+v", plan.Actions)
	}
	replanned := PlanFleet(planPolicy(small, FleetCapacity{}), planSnapshot(t, source, planHost(2, planSmall, FleetStarting)))
	if len(replanned.Actions) > 0 {
		t.Fatalf("while the replacement starts: %+v", replanned.Actions)
	}
}

func TestPlanBuysCapacityForLocationDemandAndProtectsItsHosts(t *testing.T) {
	s := planSnapshot(t, planHost(1, planSmall, FleetServing))
	s.Offers.Networks["us-west-1"] = oneZone("us-west-1a", "usw1-az1")
	s.Hosts[0].Region, s.Hosts[0].Zone, s.Hosts[0].ZoneID = "us-west-1", "us-west-1a", "usw1-az1"
	s.Locations = map[ReserveMarket][]LocationDemand{onDemand: {{Region: "us-east", Shape: small, Count: 1}}}
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), s)
	buys := actionsOf(plan, ActionBuy)
	if len(buys) != 1 || buys[0].Offer.Region != "us-east-2" || len(marketPlan(t, plan, onDemand).UnmetLocations) > 0 {
		t.Fatalf("actions %+v", plan.Actions)
	}
	s.Hosts = append(s.Hosts, planHost(2, planSmall, FleetStarting))
	if replanned := PlanFleet(planPolicy(small, FleetCapacity{}), s); len(replanned.Actions) > 0 {
		t.Fatalf("a starting east host covers it: %+v", replanned.Actions)
	}
}

func TestPlanDoesNotResumeAReserveTheLocationCannotUse(t *testing.T) {
	reserve := planHost(1, planSmall, FleetStopped)
	reserve.Region, reserve.Zone, reserve.ZoneID = "us-west-1", "us-west-1a", "usw1-az1"
	s := planSnapshot(t, reserve)
	s.Locations = map[ReserveMarket][]LocationDemand{onDemand: {{Region: "us-east", Shape: small, Count: 1}}}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	if len(actionsOf(plan, ActionResume)) > 0 || len(actionsOf(plan, ActionBuy)) != 1 {
		t.Fatalf("actions %+v", plan.Actions)
	}
}

func TestPlanRetiresAPendingReserveBeforeTheReadyOneItWouldReplace(t *testing.T) {
	ready := planHost(1, planSmall, FleetStopped)
	pending := planHost(2, planSmall, FleetPreparing)
	plan := PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, ready, pending))
	mp := marketPlan(t, plan, onDemand)
	if got := hostsOf(actionsOf(plan, ActionRetireReserve)); !slices.Equal(got, []HostID{{2}}) || mp.ReserveReady != small {
		t.Fatalf("retired %v ready %+v", got, mp.ReserveReady)
	}
}

func TestPlanAResumedReserveCannotCoverRetiringTheLastReadyOne(t *testing.T) {
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

// planFast is a small shape that hibernates.
var planFast = CatalogType{Name: "fast", CPUMillis: 8000, MemoryBytes: 16 * gib, Hibernates: true, prices: [4]int64{100_000, 100_000, 100_000, 100_000}}

func hibernatingReserve(id byte, state FleetState) FleetHost {
	h := planHost(id, planFast, state)
	h.ReserveMode = ptr(ReserveHibernate)
	return h
}

func TestPlanKeepsVerifiedHibernationOverAPlainStop(t *testing.T) {
	s := planSnapshot(t, hibernatingReserve(1, FleetImageSaved), planHost(2, planSmall, FleetStopped))
	s.Offers.Catalog = append(s.Offers.Catalog, planFast)
	plan := PlanFleet(planPolicy(FleetCapacity{}, small), s)
	mp := marketPlan(t, plan, onDemand)
	if got := hostsOf(actionsOf(plan, ActionRetireReserve)); !slices.Equal(got, []HostID{{2}}) {
		t.Fatalf("retired %v", got)
	}
	if mp.HibernationTarget != small || mp.Hibernated != small || !mp.HibernationShortfall.Empty() {
		t.Fatalf("plan %+v", mp)
	}
}

func TestPlanCountsAnUnverifiedHibernationAsReadyButNotSaved(t *testing.T) {
	s := planSnapshot(t, hibernatingReserve(1, FleetHibernateUnverified))
	s.Offers.Catalog = append(s.Offers.Catalog, planFast)
	mp := marketPlan(t, PlanFleet(planPolicy(FleetCapacity{}, small), s), onDemand)
	if mp.ReserveReady != small || mp.HibernationUnverified != small || !mp.Hibernated.Empty() || !mp.ReservePending.Empty() {
		t.Fatalf("plan %+v", mp)
	}
	plan := PlanFleet(planPolicy(small, FleetCapacity{}), s)
	if len(plan.Actions) == 0 || plan.Actions[0].Kind != ActionResume || !marketPlan(t, plan, onDemand).HibernationUnverified.Empty() {
		t.Fatalf("actions %+v", plan.Actions)
	}
}

func TestPlanBuysOneHibernatingReserveAndWaitsForIt(t *testing.T) {
	old := planHost(1, planSmall, FleetStopped)
	s := planSnapshot(t, old)
	s.Offers.Catalog = append(s.Offers.Catalog, planFast)
	p := planPolicy(FleetCapacity{}, small)
	plan := PlanFleet(p, s)
	mp := marketPlan(t, plan, onDemand)
	if got := actionsOf(plan, ActionBuyReserve); len(got) != 1 || got[0].Offer.Type.Name != "fast" || *got[0].Mode != ReserveHibernate || mp.HibernationTarget != small {
		t.Fatalf("actions %+v", plan.Actions)
	}
	if len(actionsOf(plan, ActionRetireReserve)) > 0 {
		t.Fatalf("the plain reserve retired before its replacement is ready")
	}
	s.Hosts = []FleetHost{old, hibernatingReserve(2, FleetPreparing)}
	s.Hosts[1].Current = false
	if plan := PlanFleet(p, s); len(plan.Actions) > 0 {
		t.Fatalf("while the hibernating reserve prepares: %+v", plan.Actions)
	}
	s.Hosts = []FleetHost{old, hibernatingReserve(2, FleetHibernateUnverified)}
	plan = PlanFleet(p, s)
	if got := hostsOf(plan.Actions); len(plan.Actions) != 1 || plan.Actions[0].Kind != ActionRetireReserve || got[0] != (HostID{1}) {
		t.Fatalf("once it hibernated: %+v", plan.Actions)
	}
	// EC2 refused the hibernation and it stopped plainly: it keeps its
	// hibernation slot, so nothing new is bought.
	s.Hosts = []FleetHost{hibernatingReserve(2, FleetStopped)}
	plan = PlanFleet(p, s)
	if mp := marketPlan(t, plan, onDemand); len(plan.Actions) > 0 || mp.ReserveReady != small || !mp.HibernationUnverified.Empty() || !mp.Hibernated.Empty() {
		t.Fatalf("after a plain fallback: %+v %+v", plan.Actions, mp)
	}
}

func TestSpotWorkBorrowsAnOnDemandReserveOnlyAboveItsTarget(t *testing.T) {
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
		s.Pending = []DemandGroup{{Need: Requirement{Preemptible: true, CPUMillis: 4000, MemoryBytes: 4 * gib}, Containers: []PendingContainer{{ID: uuid.New()}}}}
		plan := PlanFleet(planPolicy(FleetCapacity{}, c.target), s)
		if borrowed := len(actionsOf(plan, ActionResume)) == 1; borrowed != c.borrows {
			t.Errorf("%d reserves, target %+v: actions %+v", c.reserves, c.target, plan.Actions)
		}
	}
}

func TestPlanCountsEachHostOnceAndWarmRoomOnlyFromServingHosts(t *testing.T) {
	s := planSnapshot(t, planHost(1, planSmall, FleetServing), planHost(2, planSmall, FleetUnavailable),
		planHost(3, planSmall, FleetStarting), hibernatingReserve(4, FleetImageSaved), planHost(5, planSmall, FleetTerminating))
	s.Offers.Catalog = append(s.Offers.Catalog, planFast)
	mp := marketPlan(t, PlanFleet(DefaultPolicy(), s), onDemand)
	machines := 0
	for _, st := range mp.States {
		machines += st.Machines
	}
	if mp.WarmFree != small || mp.WarmPending != small || machines != 5 {
		t.Fatalf("free %+v pending %+v states %+v", mp.WarmFree, mp.WarmPending, mp.States)
	}
}

func pendingOne(need Requirement, host *HostID) (DemandGroup, uuid.UUID) {
	id := uuid.New()
	return DemandGroup{Need: need, Containers: []PendingContainer{{ID: id, Host: host}}}, id
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
	if w := waitOf(t, plan, id); len(plan.Actions) > 0 || *w.Wait != WaitLimit || marketPlan(t, plan, onDemand).Reason != ReasonDemandOrRecovery {
		t.Fatalf("fleet limit: %+v %+v", plan.Actions, w)
	}
}

func TestPlanCoversABatchOfContainersTogether(t *testing.T) {
	s := planSnapshot(t)
	s.Offers.Catalog = FleetCatalog()
	need := Requirement{CPUMillis: 6000, MemoryBytes: 4 * gib}
	s.Pending = []DemandGroup{{Need: need, Containers: []PendingContainer{{ID: uuid.New()}, {ID: uuid.New()}}}}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	if got := boughtTypes(plan, ActionBuy); !slices.Equal(got, []string{"c6a.4xlarge"}) || len(plan.Actions[0].Containers) != 2 {
		t.Fatalf("two 6 vCPU containers: %+v", plan.Actions)
	}
}

func TestPlanReturnsALeavingHostToTheReserveWhileTheReserveFallsShort(t *testing.T) {
	plain := PlanFleet(planPolicy(FleetCapacity{}, small), planSnapshot(t, idle(planHost(1, planSmall, FleetServing))))
	if got := actionsOf(plain, ActionReturnToReserve); len(got) != 1 || *got[0].Mode != ReserveStop || len(plain.Actions) != 1 {
		t.Fatalf("plain: %+v", plain.Actions)
	}
	leaving := idle(planHost(1, planFast, FleetServing))
	leaving.HibernationConfigured = true
	s := planSnapshot(t, leaving)
	s.Offers.Catalog = append(s.Offers.Catalog, planFast)
	plan := PlanFleet(planPolicy(FleetCapacity{}, small), s)
	if got := actionsOf(plan, ActionReturnToReserve); len(got) != 1 || *got[0].Mode != ReserveHibernate || len(plan.Actions) != 1 {
		t.Fatalf("hibernation configured: %+v", plan.Actions)
	}
	s.Hosts = append(s.Hosts, hibernatingReserve(2, FleetImageSaved))
	plan = PlanFleet(planPolicy(FleetCapacity{}, small), s)
	if got := hostsOf(actionsOf(plan, ActionDrain)); !slices.Equal(got, []HostID{{1}}) || len(plan.Actions) != 1 {
		t.Fatalf("reserve already held: %+v", plan.Actions)
	}
}

func TestPlanKeepsIdleHostsUntilBilledAndLightLongEnough(t *testing.T) {
	young := idle(planHost(1, planSmall, FleetServing))
	young.LaunchedAt = ptr(offerNow.Add(-30 * time.Second))
	fresh := planHost(2, planSmall, FleetServing)
	fresh.LightSince = ptr(offerNow.Add(-5 * time.Minute))
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), planSnapshot(t, young, fresh))
	if len(actionsOf(plan, ActionDrain)) > 0 {
		t.Fatalf("drained %+v", plan.Actions)
	}
	if got := plan.LightSince[fresh.ID]; !got.Equal(*fresh.LightSince) {
		t.Fatalf("light since %s", got)
	}
}

func TestPlanQuietMarketsReleaseTheLargestIdleHostFirstAndLoadedOnesTheSmallest(t *testing.T) {
	same := func(h FleetHost) FleetHost {
		h.HourlyMicros = ptr(int64(100_000))
		return idle(h)
	}
	quiet := PlanFleet(planPolicy(small, FleetCapacity{}), planSnapshot(t, same(planHost(1, planSmall, FleetServing)), same(planHost(2, planLarge, FleetServing))))
	if got := hostsOf(actionsOf(quiet, ActionDrain)); !slices.Equal(got, []HostID{{2}}) {
		t.Fatalf("quiet drained %v", got)
	}
	busy := planHost(3, planLarge, FleetServing)
	busy.Load, busy.Containers, busy.Pinned = cpuGiB(16_000, 8), 1, 1
	loaded := PlanFleet(planPolicy(small, FleetCapacity{}), planSnapshot(t, same(planHost(1, planSmall, FleetServing)), same(planHost(2, planLarge, FleetServing)), busy))
	if got := hostsOf(actionsOf(loaded, ActionDrain)); len(got) == 0 || got[0] != (HostID{1}) {
		t.Fatalf("loaded drained %v", got)
	}
}

func TestPlanCapsGrowthPerMarketAndPass(t *testing.T) {
	plan := PlanFleet(planPolicy(large.Times(20), FleetCapacity{}), planSnapshot(t))
	mp := marketPlan(t, plan, onDemand)
	if n := len(actionsOf(plan, ActionBuy)); n != 16 || mp.Reason != ReasonActionCap || mp.Shortfall.Empty() {
		t.Fatalf("%d buys, reason %q, short %+v", n, mp.Reason, mp.Shortfall)
	}
}

func TestAnyGPUDemandGoesToTheModelTheFleetHoldsThenTheCheapestPerCard(t *testing.T) {
	need := Requirement{GPUs: []string{GPUAny}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 8 * gib}
	s := planSnapshot(t)
	s.Offers.Catalog = FleetCatalog()
	g, _ := pendingOne(need, nil)
	s.Pending = []DemandGroup{g}
	plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	if buys := actionsOf(plan, ActionBuy); len(buys) != 1 || buys[0].Market != (ReserveMarket{GPU: "T4"}) || buys[0].Offer.Type.GPU != "T4" {
		t.Fatalf("no stock: %+v", plan.Actions)
	}
	l4 := planHost(1, mustType(t, "g6.2xlarge"), FleetServing)
	l4.GPU, l4.Load = "L4", l4.Usable
	s.Hosts = []FleetHost{l4}
	plan = PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s)
	if buys := actionsOf(plan, ActionBuy); len(buys) != 1 || buys[0].Market != (ReserveMarket{GPU: "L4"}) || buys[0].Offer.Type.GPU != "L4" {
		t.Fatalf("L4 in stock: %+v", plan.Actions)
	}
}

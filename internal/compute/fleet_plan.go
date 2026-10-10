package compute

import (
	"cmp"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
)

// Reserve states of a platform host, beside the serving ones in
// fleet_admin.go.
const (
	FleetPreparing           FleetState = "preparing"
	FleetStopping            FleetState = "stopping"
	FleetStopped             FleetState = "stopped"
	FleetHibernateUnverified FleetState = "hibernate_unverified"
	FleetImageSaved          FleetState = "image_saved"
)

// FleetHost is one platform host as the planner sees it.
type FleetHost struct {
	ID           HostID
	InstanceType string
	Region       string
	Zone         string
	ZoneID       string
	Market       Market
	GPU          string
	Usable       FleetCapacity
	State        FleetState
	// Load is what its live containers reserve; Lent, on an on-demand CPU
	// host, what those that could run on Spot reserve, which counts as the
	// Spot market's load.
	Load, Lent FleetCapacity
	Containers int
	// Protected is an interruption's source or replacement.
	Protected bool
	// Current is set on a reserve prepared for the current agent release.
	Current bool
	// ReserveMode is set on a reserve; nil on a serving host.
	ReserveMode *ReserveMode
	// HibernationConfigured is set when the instance launched able to
	// hibernate.
	HibernationConfigured bool
	// Stoppable is set when EC2 can stop the instance: on-demand, or Spot
	// on a persistent request. A one-time Spot host can only terminate.
	Stoppable bool
	// HourlyMicros is the complete hourly cost; nil when unknown.
	HourlyMicros *int64
	// IdleSince is when the serving host became idle, as last recorded;
	// BusySince when its newest live container was placed.
	IdleSince, BusySince *time.Time
	// PhaseAt is when the host entered its phase; a serving host's is when
	// it became ready.
	PhaseAt time.Time
	// Replaces is the idle host a rightsize bought this one to replace.
	Replaces *HostID
	// RightsizeRefusedAt is when EC2 last refused a launch to replace it.
	RightsizeRefusedAt *time.Time
	// Slept is set on a host that has proved it can stop into the reserve:
	// a reserve, or one resumed from it.
	Slept bool
}

func (h FleetHost) market() ReserveMarket { return reserveMarketOf(h.Market, h.GPU) }

func (h FleetHost) free() FleetCapacity { return h.Usable.Minus(h.Load).Clamp() }

// resumable is a stopped reserve.
func (h FleetHost) resumable() bool {
	return h.State == FleetStopped || h.State == FleetHibernateUnverified || h.State == FleetImageSaved
}

// reserve is a host held, or being made, a reserve.
func (h FleetHost) reserve() bool {
	return h.resumable() || h.State == FleetPreparing || h.State == FleetStopping
}

// capacity is the host as placement sees it.
func (h FleetHost) capacity() HostCapacity {
	free := h.free()
	return HostCapacity{
		Host: h.ID, Kind: KindPlatform, Provider: ProviderAWS, Region: h.Region, Zone: h.Zone, ZoneID: h.ZoneID,
		Market: h.Market, GPUType: h.GPU, GPUCount: h.Usable.GPUs,
		CPUMillis: h.Usable.CPUMillis, MemoryBytes: h.Usable.MemoryBytes,
		FreeCPUMillis: free.CPUMillis, FreeMemoryBytes: free.MemoryBytes, FreeGPUs: free.GPUs,
	}
}

// DemandGroup is pending platform containers with one requirement.
type DemandGroup struct {
	Need       Requirement
	Containers []PendingContainer
}

// PendingContainer is one pending container and the host bought for it.
type PendingContainer struct {
	ID   uuid.UUID
	Host *HostID
}

// FleetSnapshot is everything one planning pass reads.
type FleetSnapshot struct {
	Now     time.Time
	Hosts   []FleetHost
	Pending []DemandGroup
	// Recent is the largest shape each market's placed containers reserved
	// within the policy's LargestShape window, and Builds the largest its
	// finished build containers reserved within its BuildWindow; Arrived is
	// what the CPU Spot market's placed containers created within the
	// ArrivalWindow reserve, less their largest batch.
	Recent, Builds, Arrived map[ReserveMarket]FleetCapacity
	Offers                  OfferInputs
	// HostRoom is how many more hosts may run or start, reserves being
	// prepared among them; ReserveRoom how many more may be held stopped.
	HostRoom    int
	ReserveRoom int
	// BatchWait is how long the arrival batch stays open; zero once it has
	// closed.
	BatchWait time.Duration
	// FloorShortSince is when each market's stopped target went short, from
	// the last published plan.
	FloorShortSince map[ReserveMarket]time.Time
	// Peaks are each market's peak load, from the published plans.
	Peaks map[ReserveMarket]LoadPeak
}

// FleetActionKind is what an action asks for.
type FleetActionKind string

const (
	// ActionResume starts a stopped reserve to serve.
	ActionResume FleetActionKind = "resume"
	// ActionBuy launches a host to serve.
	ActionBuy FleetActionKind = "buy"
	// ActionBuyReserve launches a host that prepares and stops as a
	// reserve.
	ActionBuyReserve FleetActionKind = "buy_reserve"
	// ActionReturnToReserve stops an idle serving host into the reserve.
	ActionReturnToReserve FleetActionKind = "return_to_reserve"
	// ActionDrain drains an idle serving host and terminates it.
	ActionDrain FleetActionKind = "drain"
	// ActionRetireReserve terminates a reserve.
	ActionRetireReserve FleetActionKind = "retire_reserve"
	// ActionRightsize buys Offer to replace an idle Host; the host stays
	// until the replacement serves.
	ActionRightsize FleetActionKind = "rightsize"
)

// FleetAction is one decision, naming a host or an offer.
type FleetAction struct {
	Kind   FleetActionKind
	Market ReserveMarket
	Host   *HostID
	Offer  *FleetOffer
	Mode   *ReserveMode
	// Containers are the pending containers a resume or purchase is for.
	Containers []uuid.UUID
	// Holds is what a purchase must hold in whichever pool it launches:
	// the containers and warm slots the cover placed on it, or the reserve
	// room it was bought for.
	Holds FleetCapacity
}

// ContainerWait is why a pending container waits for compute: nil Wait
// means ready capacity takes it, or nothing can yet. A provisioning wait
// names the host or the Buy action that will take it.
type ContainerWait struct {
	Container uuid.UUID
	Wait      *CapacityWait
	Host      *HostID
	Action    *int
}

// MarketReason says why a market falls short.
type MarketReason string

const (
	ReasonNone MarketReason = ""
	// ReasonBatch holds purchases while containers keep arriving.
	ReasonBatch MarketReason = "purchases wait for arrivals to settle"
	// ReasonDemand holds headroom of its own while the market's work waits:
	// warm slots take only room bought for the work, and reserves wait.
	ReasonDemand MarketReason = "headroom waits for pending work"
	// ReasonReturning holds reserve purchases while hosts that may return
	// to the reserve cover what it lacks.
	ReasonReturning MarketReason = "reserves wait for hosts to return"
	// ReasonActionCap defers the rest of the growth to the next pass.
	ReasonActionCap MarketReason = "growth continues next pass"
	// ReasonFleetLimit means the fleet's host limit holds growth back.
	ReasonFleetLimit MarketReason = "fleet host limit reached"
	// ReasonCooldown means the offers that would cover the shortfall cool
	// after refusals.
	ReasonCooldown MarketReason = "offers cooling after refusals"
	// ReasonQuota means the vCPU quotas leave no room for an offer that
	// covers it.
	ReasonQuota MarketReason = "vCPU quota reached"
	// ReasonMargin means no offer that covers it keeps the purchase margin.
	ReasonMargin MarketReason = "no offer keeps the purchase margin"
	// ReasonNoOffer means no offer covers it.
	ReasonNoOffer MarketReason = "no offer fits"
)

// LoadPeak is the most load a market held and when it last reached it.
type LoadPeak struct {
	Load FleetCapacity `json:"load"`
	At   time.Time     `json:"at"`
}

// after is the load peak once a pass sees load: the stored one while
// within the horizon, raised to load, and restarted when load reaches it.
// Its time is to the minute, so load held at its peak republishes it at
// most once a minute.
func (p LoadPeak) after(load FleetCapacity, now time.Time, horizon time.Duration) LoadPeak {
	if now.Sub(p.At) >= horizon {
		p = LoadPeak{}
	}
	switch {
	case load.Empty():
		return p
	case load.Covers(p.Load):
		return LoadPeak{Load: load, At: now.Truncate(time.Minute)}
	}
	return LoadPeak{Load: p.Load.Upper(load), At: p.At}
}

// MarketPlan is one market's targets and measures.
type MarketPlan struct {
	Market ReserveMarket
	// Load is what the market's containers hold now: running and pending.
	Load FleetCapacity
	// WarmTarget is what the market's warm slots total.
	WarmTarget, WarmFree, WarmPending FleetCapacity
	StoppedTarget                     FleetCapacity
	ReserveReady, ReservePending      FleetCapacity
	// Shortfall is the warm slots nothing holds room for; StoppedShortfall
	// what the reserves lack.
	Shortfall, StoppedShortfall FleetCapacity
	// FloorShortSince is when the stopped target went short; nil while held.
	FloorShortSince *time.Time
	// Peak is the most load the market held within the cost horizon; its
	// reserves stay while it holds.
	Peak   LoadPeak
	Reason MarketReason
	States []FleetStateCapacity
}

// FleetPlan is one pass's decisions.
type FleetPlan struct {
	Markets []MarketPlan
	Actions []FleetAction
	Waits   []ContainerWait
	// IdleSince is when each idle serving host became idle.
	IdleSince map[HostID]time.Time
	// BatchWait is how long the purchases the pass held wait for arrivals
	// to settle; zero when it held none.
	BatchWait time.Duration
}

// PlanFleet decides one pass over a snapshot. One cover packs pending
// containers and every market's warm slots: onto ready room, then starting
// hosts, then reserves resumed for them, then purchases, which wait while
// containers keep arriving. Idle hosts that hold no slot leave, into the
// reserve while it falls short. Each market then buys what its reserves
// lack or retires their surplus, and rightsizes an idle host a cheaper type
// could replace.
func PlanFleet(p Policy, s FleetSnapshot) FleetPlan {
	ps := &pass{
		p: p, s: s, hosts: slices.Clone(s.Hosts),
		used: map[ReserveMarket]int{}, waiting: map[ReserveMarket]bool{}, limited: map[ReserveMarket]bool{}, batched: map[ReserveMarket]bool{},
		hostRoom: s.HostRoom, reserveRoom: s.ReserveRoom, offers: map[string][]FleetOffer{}, byMarket: map[ReserveMarket]*marketView{},
		claimed: map[HostID]bool{}, holding: map[HostID][]warmSlot{}, plan: FleetPlan{IdleSince: map[HostID]time.Time{}},
		quotaUsed: QuotaUse(s.Hosts, s.Offers.Catalog), spot: freshSpot(p, s.Offers.Spot, s.Now),
	}
	ps.s.Offers.QuotaUsed = maps.Clone(ps.quotaUsed)
	items := ps.pendingItems()
	views := ps.views(items)
	ps.cover(slices.Concat(items, slotItems(views)))
	ps.idle()
	for _, v := range views {
		ps.retain(v)
		ps.reserves(v)
		ps.rightsize(v)
		ps.plan.Markets = append(ps.plan.Markets, ps.report(v))
	}
	return ps.plan
}

// pass is one planning pass's working state.
type pass struct {
	p     Policy
	s     FleetSnapshot
	hosts []FleetHost
	plan  FleetPlan
	// planned are hosts bought in this pass.
	planned []plannedHost
	used    map[ReserveMarket]int
	// waiting are markets with pending work no ready host holds, limited
	// those whose work the fleet limit holds back and batched those whose
	// purchases wait for arrivals to settle.
	waiting, limited, batched map[ReserveMarket]bool
	hostRoom, reserveRoom     int
	offers                    map[string][]FleetOffer
	byMarket                  map[ReserveMarket]*marketView
	// claimed are serving hosts pending work fits; holding are the warm
	// slots each serving host keeps room for.
	claimed map[HostID]bool
	holding map[HostID][]warmSlot
	// quotaUsed is what running hosts and this pass's starts count
	// against each vCPU quota.
	quotaUsed map[QuotaKey]int64
	// spot is the freshest quote of each Spot pool.
	spot map[spotPool]SpotQuote
}

type plannedHost struct {
	market  ReserveMarket
	offer   FleetOffer
	reserve bool
}

// marketView is one market during the pass.
type marketView struct {
	m     ReserveMarket
	load  FleetCapacity
	slots []warmSlot
	// warm is the warm target the slots hold, but a build's.
	warm FleetCapacity
	// stopped is the reserve target purchases keep, a share of the load,
	// and peakShare the same share of the peak, which reserves woken for a
	// burst return to.
	stopped, peakShare FleetCapacity
	// peak is the market's load peak; while it holds, the reserves a burst
	// bought or woke wait for the next one.
	peak LoadPeak
	// largest is the shape one of the market's reserves fits; empty for
	// none. loaded is set while the share of load, not the floor beside
	// the largest shape, sets the stopped target.
	largest FleetCapacity
	loaded  bool
	retired map[HostID]bool
	// shortfall and stoppedShort are what the pass could not cover, and
	// warmReason and stoppedReason why.
	shortfall, stoppedShort   FleetCapacity
	warmReason, stoppedReason MarketReason
	// floorShortSince is when the stopped target went short; nil while held.
	floorShortSince *time.Time
}

func (ps *pass) inMarket(m ReserveMarket, keep func(FleetHost) bool) []*FleetHost {
	var out []*FleetHost
	for i := range ps.hosts {
		if ps.hosts[i].market() == m && keep(ps.hosts[i]) {
			out = append(out, &ps.hosts[i])
		}
	}
	return out
}

func serving(h FleetHost) bool  { return h.State == FleetServing }
func starting(h FleetHost) bool { return h.State == FleetStarting }

// coverItem is one pending container or warm slot during the cover.
type coverItem struct {
	// slot is set on a warm slot, which has no container and no wait, and
	// kind says why the market keeps it.
	slot   bool
	kind   slotKind
	id     uuid.UUID
	need   Requirement
	market ReserveMarket
	bought *HostID
	wait   int
}

// buysWithWork reports a warm slot of the CPU Spot market that follows its
// load and arrivals: it is room the next arrivals find rather than wait a
// launch for, so it is bought or resumed with the work rather than riding
// on it.
func (it coverItem) buysWithWork() bool {
	return it.slot && it.kind == slotLoad && it.market.spotCPU()
}

// pendingItems lists the pending containers, each with a wait slot and the
// market its demand belongs to: those a host was bought for first, so other
// work never takes their room, then work that cannot run on Spot, as
// placement orders it, then largest first.
func (ps *pass) pendingItems() []coverItem {
	var items []coverItem
	for _, g := range ps.s.Pending {
		m := ps.demandMarket(g.Need)
		for _, c := range g.Containers {
			ps.plan.Waits = append(ps.plan.Waits, ContainerWait{Container: c.ID})
			items = append(items, coverItem{id: c.ID, need: g.Need, market: m, bought: c.Host, wait: len(ps.plan.Waits) - 1})
		}
	}
	slices.SortStableFunc(items, func(a, b coverItem) int {
		return cmp.Or(boolOrder(a.bought == nil, b.bought == nil), boolOrder(a.need.Preemptible, b.need.Preemptible), size(b.need)-size(a.need))
	})
	return items
}

// views sets every market's targets from its current load, what its hosts'
// containers reserve and what its pending containers ask for: its warm
// slots, with one of the shape of its builds while they run in a CPU
// market, since a GPU host held warm for builds costs far more, and its
// stopped target: its share of load, and at least its floor beside the
// largest shape one reserve must fit.
func (ps *pass) views(items []coverItem) []*marketView {
	markets := ps.p.Markets()
	known := len(markets)
	add := func(m ReserveMarket) {
		if !slices.Contains(markets, m) {
			markets = append(markets, m)
		}
	}
	for _, h := range ps.hosts {
		add(h.market())
	}
	for _, it := range items {
		add(it.market)
	}
	slices.SortStableFunc(markets[known:], func(a, b ReserveMarket) int { return strings.Compare(a.String(), b.String()) })
	views := make([]*marketView, 0, len(markets))
	for _, m := range markets {
		v := &marketView{m: m, retired: map[HostID]bool{}}
		v.load = ps.marketLoad(m)
		largest := ps.s.Recent[m]
		for _, it := range items {
			if it.market == m {
				v.load = v.load.Plus(reservedShape(it.need))
				largest = largest.Upper(reservedShape(it.need))
			}
		}
		r := ps.p.Reserve(m)
		// The Spot market keeps warm at least the work that came at a
		// steady rate.
		v.warm = r.Warm.Of(v.load)
		if m.spotCPU() && r.Warm != (HeadroomTarget{}) {
			v.warm = v.warm.Upper(r.Warm.Floor.Plus(ps.s.Arrived[m]))
		}
		v.slots = ps.p.slots(r.Warm, v.warm)
		if build := ps.s.Builds[m]; !build.Empty() && m.GPU == "" && r.Warm != (HeadroomTarget{}) {
			v.slots = append(v.slots, warmSlot{shape: build.Lower(ps.p.LargestShape.Cap), kind: slotBuild})
		}
		v.peak = ps.s.Peaks[m].after(v.load, ps.s.Now, ps.p.CostHorizon)
		v.stopped, v.peakShare = r.Stopped.Of(v.load), r.Stopped.Of(v.peak.Load)
		if r.FitLargest {
			v.largest = ps.p.LargestShape.of(m, largest)
			floor := r.Stopped.Floor.Plus(v.largest)
			v.loaded = !floor.Covers(v.stopped)
			v.stopped, v.peakShare = v.stopped.Upper(floor), v.peakShare.Upper(floor)
		}
		ps.byMarket[m] = v
		views = append(views, v)
	}
	return views
}

// marketLoad is what market m's work holds on running hosts: its hosts'
// containers, less what Spot-tolerant work borrows on them, and for the
// CPU Spot market what it borrows on on-demand hosts.
func (ps *pass) marketLoad(m ReserveMarket) FleetCapacity {
	var load FleetCapacity
	for _, h := range ps.hosts {
		switch {
		case h.market() == m:
			load = load.Plus(h.Load.Minus(h.Lent))
		case m.spotCPU():
			load = load.Plus(h.Lent)
		}
	}
	return load
}

// slotItems are every market's warm slots as cover items, largest first.
func slotItems(views []*marketView) []coverItem {
	var out []coverItem
	for _, v := range views {
		for _, slot := range v.slots {
			out = append(out, coverItem{slot: true, kind: slot.kind, need: slotNeed(v.m, slot.shape), market: v.m, wait: -1})
		}
	}
	slices.SortStableFunc(out, func(a, b coverItem) int { return size(b.need) - size(a.need) })
	return out
}

// marketNeed is what work of market m asks of an offer.
func marketNeed(m ReserveMarket) Requirement {
	need := Requirement{Preemptible: m.Preemptible}
	if m.GPU != "" {
		need.GPUs = []string{m.GPU}
	}
	return need
}

// slotNeed is a warm slot of market m as a requirement; a GPU market's
// slot holds at least one card.
func slotNeed(m ReserveMarket, shape FleetCapacity) Requirement {
	need := marketNeed(m)
	need.CPUMillis, need.MemoryBytes, need.GPUCount = shape.CPUMillis, shape.MemoryBytes, shape.GPUs
	return need
}

// reservedShape is what a requirement reserves on a host.
func reservedShape(r Requirement) FleetCapacity {
	return FleetCapacity{CPUMillis: r.CPUMillis, MemoryBytes: r.MemoryBytes, GPUs: r.GPUsNeeded()}
}

// marketOffers are the offers a market's own growth buys from.
func (ps *pass) marketOffers(m ReserveMarket, reserve bool) []FleetOffer {
	key := m.String() + "/" + boolKey(reserve)
	if offers, ok := ps.offers[key]; ok {
		return offers
	}
	offers := slices.DeleteFunc(RankOffers(ps.p, marketNeed(m), reserve, ps.s.Offers), func(o FleetOffer) bool {
		return o.Market != m.buyMarket() || o.Type.GPU != m.GPU
	})
	ps.offers[key] = offers
	return offers
}

func boolKey(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

// typeNamed finds a type in the snapshot's catalog.
func (ps *pass) typeNamed(name string) (CatalogType, bool) {
	i := slices.IndexFunc(ps.s.Offers.Catalog, func(t CatalogType) bool { return t.Name == name })
	if i < 0 {
		return CatalogType{}, false
	}
	return ps.s.Offers.Catalog[i], true
}

// stoppedMicros is what a host costs stopped: its root disk.
func (ps *pass) stoppedMicros(h FleetHost) int64 {
	t, ok := ps.typeNamed(h.InstanceType)
	if !ok {
		return 0
	}
	return rootDiskMicros(h.Region, t.RootGiB(h.ReserveMode != nil && *h.ReserveMode == ReserveHibernate), t.PricedMiBps(h.ReserveMode != nil))
}

func (ps *pass) cooled(h FleetHost) bool {
	return cooled(ps.s.Offers.Cooldowns, ps.s.Now, h.Region, h.ZoneID, h.InstanceType, h.Market)
}

func (ps *pass) room(m ReserveMarket) int { return ps.p.MaxGrowthActions - ps.used[m] }

// batching reports whether purchases wait for arrivals to settle, and
// records that one waited.
func (ps *pass) batching() bool {
	if ps.s.BatchWait <= 0 {
		return false
	}
	ps.plan.BatchWait = ps.s.BatchWait
	return true
}

// limits bounds a cover to nodes and the quota room left.
func (ps *pass) limits(nodes int) CoverLimits {
	return CoverLimits{Nodes: nodes, VCPUs: quotaRoom(ps.s.Offers.Quotas, ps.quotaUsed)}
}

// quotaOf is the quota a host of type counts against, and its vCPUs.
func (ps *pass) quotaOf(region, instanceType string, market Market) (QuotaKey, int64) {
	t, _ := ps.typeNamed(instanceType)
	class, _ := QuotaClassOf(instanceType)
	return QuotaKey{Region: region, Class: class, Market: market}, t.VCPUs()
}

// startable reports whether a stopped host's quota has room to start it.
func (ps *pass) startable(h FleetHost) bool {
	key, vcpus := ps.quotaOf(h.Region, h.InstanceType, h.Market)
	left, known := quotaRoom(ps.s.Offers.Quotas, ps.quotaUsed)[key]
	return !known || left >= vcpus
}

func (ps *pass) start(region, instanceType string, market Market) {
	key, vcpus := ps.quotaOf(region, instanceType, market)
	ps.quotaUsed[key] += vcpus
}

func (ps *pass) act(a FleetAction) int {
	switch a.Kind {
	case ActionResume, ActionBuy, ActionBuyReserve, ActionRightsize:
		ps.used[a.Market]++
	case ActionReturnToReserve, ActionDrain, ActionRetireReserve:
	}
	ps.plan.Actions = append(ps.plan.Actions, a)
	return len(ps.plan.Actions) - 1
}

// buy launches a host from o, to serve or as a reserve in its sleep mode.
func (ps *pass) buy(m ReserveMarket, o FleetOffer, kind FleetActionKind, holds FleetCapacity, containers []uuid.UUID, replaces *HostID) int {
	reserve := kind == ActionBuyReserve
	var mode *ReserveMode
	if reserve {
		mode = ptr(ReserveStop)
		if o.Hibernate {
			mode = ptr(ReserveHibernate)
		}
		ps.reserveRoom--
	}
	// A reserve runs while it is prepared, so it takes host room too.
	ps.hostRoom--
	ps.planned = append(ps.planned, plannedHost{market: reserveMarketOf(o.Market, o.Type.GPU), offer: o, reserve: reserve})
	ps.start(o.Region, o.Type.Name, o.Market)
	return ps.act(FleetAction{Kind: kind, Market: m, Host: replaces, Offer: &o, Mode: mode, Containers: containers, Holds: holds})
}

func (ps *pass) resume(m ReserveMarket, h *FleetHost, kind FleetActionKind, containers []uuid.UUID) int {
	h.State = FleetStarting
	ps.start(h.Region, h.InstanceType, h.Market)
	ps.hostRoom--
	ps.reserveRoom++
	return ps.act(FleetAction{Kind: kind, Market: m, Host: ptr(h.ID), Containers: containers})
}

// size orders requirements largest first: GPUs dominate, then CPU and
// memory in comparable units.
func size(r Requirement) int {
	return r.GPUsNeeded()<<40 + int(r.CPUMillis) + int(r.MemoryBytes>>20)/4
}

// bin is a host the cover packs onto and the room it has left.
type bin struct {
	host *FleetHost
	room HostCapacity
}

// takes reports whether the bin's room holds it; a slot takes only a host
// of its own market.
func (b bin) takes(it coverItem) bool {
	return (!it.slot || b.host.market() == it.market) && b.room.Fits(it.need)
}

// take reserves it on a bin and returns its index, or -1: a slot on the
// first bin of its market with room, a pending container on the one
// ChooseHost picks, as placement would. rooms mirror the bins' room.
func take(bins []bin, rooms []HostCapacity, it coverItem, floor FleetCapacity) int {
	var i int
	if it.slot {
		i = slices.IndexFunc(bins, func(b bin) bool { return b.takes(it) })
	} else {
		i = ChooseHost(rooms, it.need, floor)
	}
	if i >= 0 {
		bins[i].room.Reserve(it.need)
		rooms[i] = bins[i].room
	}
	return i
}

// held is the room host id's warm slots hold, and holds whether it holds
// a slot of one of kinds.
func (ps *pass) held(id HostID) FleetCapacity {
	return totalOf(ps.holding[id], func(s warmSlot) FleetCapacity { return s.shape })
}

func (ps *pass) holds(id HostID, kinds ...slotKind) bool {
	return slices.ContainsFunc(ps.holding[id], func(s warmSlot) bool { return slices.Contains(kinds, s.kind) })
}

// binRooms are the bins' room.
func binRooms(bins []bin) []HostCapacity {
	rooms := make([]HostCapacity, len(bins))
	for i, b := range bins {
		rooms[i] = b.room
	}
	return rooms
}

// cover packs pending containers, then warm slots, in one pass. A pending
// container takes room on the ready host placement would give it, a slot
// room on a ready host of its market, busy ones first and the cheapest
// idle ones after, a resumed reserve last so it can go back, and a
// rightsize's replacement before the host it replaces; a pending container
// then the host bought for it; each then a starting host. Ready reserves resume for what is
// left, and purchases cover the rest.
func (ps *pass) cover(items []coverItem) {
	var ready, coming []bin
	for i := range ps.hosts {
		h := &ps.hosts[i]
		switch h.State {
		case FleetServing:
			ready = append(ready, bin{host: h, room: h.capacity()})
		case FleetStarting:
			coming = append(coming, bin{host: h, room: h.capacity()})
		case FleetDraining, FleetUnavailable, FleetTerminating, FleetPreparing, FleetStopping, FleetStopped,
			FleetHibernateUnverified, FleetImageSaved:
		}
	}
	slices.SortStableFunc(ready, func(a, b bin) int {
		idleA, idleB := a.host.Containers == 0, b.host.Containers == 0
		if a.host.Slept != b.host.Slept || !idleA || !idleB {
			return cmp.Or(boolOrder(a.host.Slept, b.host.Slept), boolOrder(idleA, idleB))
		}
		return ps.retention(a.host, b.host)
	})
	for i := 0; i < len(ready); i++ {
		r := ready[i].host.Replaces
		if j := slices.IndexFunc(ready[:i], func(b bin) bool { return r != nil && b.host.ID == *r }); j >= 0 {
			moved := ready[i]
			copy(ready[j+1:i+1], ready[j:i])
			ready[j] = moved
		}
	}
	// readyRooms and comingRooms are the bins' room, which pending
	// containers take as placement would: by ChooseHost.
	readyRooms, comingRooms := binRooms(ready), binRooms(coming)
	floor := ps.p.OnDemand.Warm.Floor
	var left []coverItem
	for _, it := range items {
		if it.need.Machine != "" || it.need.Connection != nil {
			continue
		}
		if i := take(ready, readyRooms, it, floor); i >= 0 {
			h := ready[i].host
			if it.slot {
				ps.holding[h.ID] = append(ps.holding[h.ID], warmSlot{shape: reservedShape(it.need), kind: it.kind})
				continue
			}
			// The room it takes is load, not warm headroom.
			h.Load = h.Load.Plus(reservedShape(it.need))
			ps.claimed[h.ID] = true
			continue
		}
		if !it.slot {
			ps.waiting[it.market] = true
		}
		if it.bought != nil {
			if i := slices.IndexFunc(coming, func(b bin) bool { return b.host.ID == *it.bought }); i >= 0 {
				coming[i].room.Reserve(it.need)
				comingRooms[i] = coming[i].room
				ps.provisioning(it, ptr(coming[i].host.ID), nil)
				continue
			}
		}
		if i := take(coming, comingRooms, it, floor); i >= 0 {
			ps.provisioning(it, ptr(coming[i].host.ID), nil)
			continue
		}
		left = append(left, it)
	}
	ps.buyFor(ps.resumeFor(left))
}

// provisioning records that a pending container waits for a host; a slot
// has no wait.
func (ps *pass) provisioning(it coverItem, host *HostID, action *int) {
	if it.slot {
		return
	}
	w := &ps.plan.Waits[it.wait]
	w.Wait, w.Host, w.Action = ptr(WaitProvisioning), host, action
}

// demandMarket is the market a container's demand belongs to: its purchase
// market for CPU work, and for GPU work the on-demand market of the model
// it would buy.
func (ps *pass) demandMarket(need Requirement) ReserveMarket {
	if need.GPUsNeeded() == 0 {
		return ReserveMarket{Preemptible: need.Preemptible}
	}
	if models := ps.gpuModels(need, ps.classOffers(need)); len(models) > 0 {
		return ReserveMarket{GPU: models[0]}
	}
	for _, model := range need.GPUs {
		if model != GPUAny {
			return ReserveMarket{GPU: model}
		}
	}
	return ReserveMarket{GPU: GPUAny}
}

// classKey is what offers depend on in a requirement.
func classKey(need Requirement) Requirement {
	return Requirement{Region: need.Region, Zone: need.Zone, Preemptible: need.Preemptible, GPUs: need.GPUs}
}

// classOffers ranks the offers for work of need's placement and GPUs, once
// per pass.
func (ps *pass) classOffers(need Requirement) []FleetOffer {
	c := classKey(need)
	key := "demand/" + c.Region + "/" + c.Zone + "/" + boolKey(c.Preemptible) + "/" + strings.Join(c.GPUs, ",")
	if offers, ok := ps.offers[key]; ok {
		return offers
	}
	offers := RankOffers(ps.p, c, false, ps.s.Offers)
	ps.offers[key] = offers
	return offers
}

// gpuModels are the GPU models work may buy, best first, among those an
// offer serves: the work's preference rank; among equal ranks a model the
// fleet holds, then the cheapest per card. "any" takes only the policy's
// reserved cards. Later models are fallbacks when earlier ones cannot
// place the work.
func (ps *pass) gpuModels(need Requirement, offers []FleetOffer) []string {
	type model struct {
		name    string
		rank    int
		stocked bool
		perCard int64
	}
	var models []model
	for _, o := range offers {
		rank := GPURank(need.GPUs, o.Type.GPU)
		if o.Type.GPU == "" || rank < 0 {
			continue
		}
		if _, reserved := ps.p.GPU[o.Type.GPU]; need.GPUs[rank] == GPUAny && !reserved {
			continue
		}
		per := o.HourlyMicros / int64(o.Type.GPUCount)
		if i := slices.IndexFunc(models, func(m model) bool { return m.name == o.Type.GPU }); i >= 0 {
			models[i].perCard = min(models[i].perCard, per)
			continue
		}
		stocked := slices.ContainsFunc(ps.hosts, func(h FleetHost) bool { return h.GPU == o.Type.GPU && h.State != FleetTerminating })
		models = append(models, model{name: o.Type.GPU, rank: rank, stocked: stocked, perCard: per})
	}
	slices.SortFunc(models, func(a, b model) int {
		return cmp.Or(cmp.Compare(a.rank, b.rank), boolOrder(!a.stocked, !b.stocked), cmp.Compare(a.perCard, b.perCard), strings.Compare(a.name, b.name))
	})
	names := make([]string, len(models))
	for i, m := range models {
		names[i] = m.name
	}
	return names
}

// resumeFor resumes ready reserves for what nothing running holds: first
// for pending containers, with warm slots riding on the room each opens,
// then for the slots left: in a market where work waits, only those that
// come with the work, since the reserves stay for it. A resume waits for
// no batch: it is faster than any purchase.
func (ps *pass) resumeFor(items []coverItem) []coverItem {
	return ps.resumeEach(ps.resumeEach(items, true), false)
}

// resumeEach resumes the best reserve for items until none takes any, and
// returns what is left; for work, a resume must take a pending container.
func (ps *pass) resumeEach(items []coverItem, work bool) []coverItem {
	for len(items) > 0 && ps.hostRoom > 0 {
		h, taken := ps.bestReserve(items, work)
		if h == nil {
			break
		}
		var ids []uuid.UUID
		for _, n := range taken {
			if !items[n].slot {
				ids = append(ids, items[n].id)
			}
		}
		ps.resume(items[taken[0]].market, h, ActionResume, ids)
		var rest []coverItem
		for n, it := range items {
			if slices.Contains(taken, n) {
				ps.provisioning(it, ptr(h.ID), nil)
				continue
			}
			rest = append(rest, it)
		}
		items = rest
	}
	return items
}

// bestReserve is the ready reserve to resume for items, and the items, by
// index, it takes first fit, pending containers before slots: one of its
// first item's market first, then the one that takes the most pending
// containers, then one that takes every pending container and every slot
// of its market, so one resume does; then the smallest, the cheapest, and
// the one that takes the most slots.
// Spot work takes an on-demand reserve only while it is lendable. Without
// work, only slots of markets where no work waits, or slots that come with
// the work, take a reserve, and only one whose absence leaves a reserve
// that fits the largest shape and that costs no more to serve than buying
// for the slots it takes.
func (ps *pass) bestReserve(items []coverItem, work bool) (*FleetHost, []int) {
	var best *FleetHost
	var bestTaken []int
	var bestCount [2]int
	all := func(h *FleetHost, taken []int) bool {
		for n, it := range items {
			if (!it.slot || it.market == h.market()) && !slices.Contains(taken, n) {
				return false
			}
		}
		return true
	}
	for i := range ps.hosts {
		h := &ps.hosts[i]
		if !ps.ready(*h) {
			continue
		}
		lends := h.Market != MarketOnDemand || ps.lendable(*h)
		b := bin{host: h, room: h.capacity()}
		var taken []int
		var count [2]int
		for kind, slot := range []bool{false, true} {
			for n, it := range items {
				eligible := it.slot == slot && ps.room(it.market) > 0 && b.takes(it)
				switch {
				case !eligible:
					continue
				case !slot && (!work || it.need.Preemptible && !lends):
					continue
				case slot && !work && (ps.waiting[it.market] && !it.buysWithWork() || ps.displaced(it)):
					continue
				}
				b.room.Reserve(it.need)
				taken = append(taken, n)
				count[kind]++
			}
		}
		if len(taken) == 0 || work && count[0] == 0 || !work && (!ps.leavesLarge(*h) || !ps.resumePays(h, items, taken)) {
			continue
		}
		if best == nil || cmp.Or(boolOrder(h.market() != items[taken[0]].market, best.market() != items[bestTaken[0]].market),
			cmp.Compare(bestCount[0], count[0]),
			boolOrder(!all(h, taken), !all(best, bestTaken)), shapeOrder(h.Usable, best.Usable), ps.retention(h, best)) < 0 {
			best, bestTaken, bestCount = h, taken, count
		}
	}
	return best, bestTaken
}

// ready is a stopped reserve prepared for the current agent release that
// can resume now.
func (ps *pass) ready(h FleetHost) bool {
	return h.resumable() && h.Current && !h.Protected && !ps.cooled(h) && ps.startable(h)
}

// leavesLarge reports whether the reserves of h's market without h still
// hold one that fits its largest shape, where h fits it.
func (ps *pass) leavesLarge(h FleetHost) bool {
	v := ps.byMarket[h.market()]
	return !h.Usable.Covers(v.largest) || ps.holdsLargest(v, h.ID)
}

// retention orders idle hosts by what keeping them costs, cheapest and
// smallest first, unknown last.
func (ps *pass) retention(a, b *FleetHost) int {
	costA, knownA := ps.hostCost(*a)
	costB, knownB := ps.hostCost(*b)
	return cmp.Or(boolOrder(!knownA, !knownB), cmp.Compare(costA, costB), cmp.Compare(a.Usable.CPUMillis, b.Usable.CPUMillis),
		cmp.Compare(a.Usable.MemoryBytes, b.Usable.MemoryBytes), strings.Compare(a.ID.String(), b.ID.String()))
}

// hostCost is what host h costs to serve over the cost horizon, priced
// as servingCost prices a purchase from the current offer inputs: its
// pool's fresh Spot quote or its on-demand price, its root and public
// address and its expected transfer. It carries no placement penalty: the
// penalty predicts whether a launch fills, and h already runs. Without a
// current price it is the price h recorded with its transfer; false when
// neither is known.
func (ps *pass) hostCost(h FleetHost) (int64, bool) {
	t, catalogued := ps.typeNamed(h.InstanceType)
	var o FleetOffer
	if catalogued {
		o.TransferMicros = transferMicros(h.Region, t)
	}
	compute, priced := t.OnDemandMicros(h.Region)
	if h.Market == MarketSpot {
		q, quoted := ps.spot[spotPool{h.Region, h.ZoneID, h.InstanceType}]
		compute, priced = q.HourlyMicros, quoted
	}
	switch {
	case catalogued && priced:
		o.HourlyMicros = compute + rootDiskMicros(h.Region, t.RootGiB(h.HibernationConfigured), t.PricedMiBps(false)) + ratesIn(h.Region).ipv4Hour
	case h.HourlyMicros != nil:
		o.HourlyMicros = *h.HourlyMicros
	default:
		return 0, false
	}
	return servingCost(ps.p)(o), true
}

// resumePays reports whether resuming reserve h for the slots it takes, by
// index into items, costs no more over the cost horizon than the cheapest
// purchase that covers them. The resume is priced without the root it pays
// stopped as well, the purchase with what it runs while it provisions.
// With no purchase that covers them, a resume is the only way.
func (ps *pass) resumePays(h *FleetHost, items []coverItem, taken []int) bool {
	var need CoverNeed
	for _, n := range taken {
		shape := reservedShape(items[n].need)
		if i := slices.IndexFunc(need.Items, func(c CoverItem) bool { return c.Shape == shape }); i >= 0 {
			need.Items[i].Count++
			continue
		}
		need.Items = append(need.Items, CoverItem{Shape: shape, Count: 1})
	}
	m := h.market()
	result := Cover(ps.marketOffers(m, false), need, servingCost(ps.p), ps.limits(min(ps.room(m), ps.hostRoom)))
	if !result.Complete(need) {
		return true
	}
	var purchase int64
	for _, node := range result.Nodes {
		purchase += servingCost(ps.p)(node.Offer) + node.Offer.HourlyMicros*int64(ps.p.Provision/time.Second)
	}
	cost, known := ps.hostCost(*h)
	return known && cost-ps.stoppedMicros(*h)*int64(ps.p.CostHorizon/time.Second) <= purchase
}

// lendable reports whether Spot work may resume on-demand reserve h: the
// ready on-demand reserves left without it still meet their stopped target
// and hold one that fits the largest shape.
func (ps *pass) lendable(h FleetHost) bool {
	v := ps.byMarket[h.market()]
	return ps.floor(v, h.ID, FleetHost.resumable).Covers(v.stopped) && ps.leavesLarge(h)
}

// demandClass is cover items that can share offers, by shape and kind.
type demandClass struct {
	need   Requirement
	market ReserveMarket
	groups []itemGroup
}

// itemGroup is a class's items of one shape and kind.
type itemGroup struct {
	shape FleetCapacity
	slot  bool
	kind  slotKind
	items []coverItem
}

// coverNeed is what is left of the class's items, and the index of each
// need item's group. A warm slot of a market where work waits rides,
// taking only room left on hosts bought for that work, unless it comes
// with the work.
func (ps *pass) coverNeed(c *demandClass) (CoverNeed, []int) {
	var need CoverNeed
	var at []int
	for i, g := range c.groups {
		if len(g.items) == 0 {
			continue
		}
		item := CoverItem{Shape: g.shape, Count: len(g.items)}
		if g.slot {
			it := g.items[0]
			item.Market, item.Rides = ptr(it.market.buyMarket()), ps.waiting[it.market] && !it.buysWithWork() || ps.displaced(it)
		}
		need.Items = append(need.Items, item)
		at = append(at, i)
	}
	return need, at
}

// displaced reports a floor slot whose room a serving host of its market
// gave to work placed less than FloorHold ago. The slot waits for that work
// to end rather than buy a host, and buys once the work outlasts the hold.
func (ps *pass) displaced(it coverItem) bool {
	return it.slot && it.kind == slotFloor && slices.ContainsFunc(ps.hosts, func(h FleetHost) bool {
		return h.market() == it.market && serving(h) && h.Usable.Covers(reservedShape(it.need)) &&
			h.BusySince != nil && ps.s.Now.Sub(*h.BusySince) < ps.p.FloorHold
	})
}

// buyFor covers what no running host or resumed reserve holds with new
// hosts, per class of items that share offers, unless containers are still
// arriving or the fleet limit is reached.
func (ps *pass) buyFor(items []coverItem) {
	var classes []*demandClass
	for _, it := range items {
		key := classKey(it.need)
		n := slices.IndexFunc(classes, func(c *demandClass) bool {
			return c.need.Region == key.Region && c.need.Zone == key.Zone && c.need.Preemptible == key.Preemptible && slices.Equal(c.need.GPUs, key.GPUs)
		})
		if n < 0 {
			classes = append(classes, &demandClass{need: key, market: it.market})
			n = len(classes) - 1
		}
		c := classes[n]
		shape := reservedShape(it.need)
		i := slices.IndexFunc(c.groups, func(g itemGroup) bool { return g.shape == shape && g.slot == it.slot && g.kind == it.kind })
		if i < 0 {
			c.groups = append(c.groups, itemGroup{shape: shape, slot: it.slot, kind: it.kind})
			i = len(c.groups) - 1
		}
		c.groups[i].items = append(c.groups[i].items, it)
	}
	for _, c := range classes {
		held := ps.hostRoom > 0 && ps.batching()
		if ps.hostRoom > 0 && !held {
			offers := ps.classOffers(c.need)
			markets := []ReserveMarket{c.market}
			if c.market.GPU != "" {
				markets = nil
				for _, model := range ps.gpuModels(c.need, offers) {
					markets = append(markets, ReserveMarket{GPU: model})
				}
			}
			for _, m := range markets {
				ps.buyClass(c, m, offers)
			}
		}
		for _, g := range c.groups {
			for _, it := range g.items {
				if it.slot {
					v := ps.byMarket[it.market]
					v.shortfall = v.shortfall.Plus(g.shape)
					switch {
					case v.warmReason != ReasonNone:
					case ps.waiting[it.market]:
						v.warmReason = ReasonDemand
					default:
						v.warmReason = ps.shortReason(it.market, g.shape, false)
					}
					continue
				}
				switch {
				case ps.hostRoom <= 0:
					ps.plan.Waits[it.wait].Wait = ptr(WaitLimit)
					ps.limited[c.market] = true
				case held:
					ps.batched[c.market] = true
				}
			}
		}
	}
}

// buyClass covers what is left of class c with the offers of market m:
// pending containers and warm slots on the same new hosts. The CPU Spot
// market buys Spot hosts that each hold at least its warm target, up to
// the largest shape's cap: Spot costs less per core on larger hosts, so a
// growing market buys hosts that grow with it, and the room they leave is
// headroom. What those cannot take, under a quota or a cooldown, any offer
// covers. On-demand prices scale with size, so on-demand hosts hold what
// the cover needs.
func (ps *pass) buyClass(c *demandClass, m ReserveMarket, offers []FleetOffer) {
	if m.GPU != "" {
		ps.coverClass(c, m, slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool { return o.Type.GPU != m.GPU }))
		return
	}
	if v := ps.byMarket[m]; v != nil && m.spotCPU() {
		least := v.warm.Lower(ps.p.LargestShape.Cap)
		ps.coverClass(c, m, slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool {
			return o.Market != MarketSpot || !o.Usable.Covers(least)
		}))
	}
	ps.coverClass(c, m, offers)
}

// coverClass buys the cheapest cover of what is left of class c from
// offers.
func (ps *pass) coverClass(c *demandClass, m ReserveMarket, offers []FleetOffer) {
	need, at := ps.coverNeed(c)
	if len(at) == 0 {
		return
	}
	result := Cover(offers, need, servingCost(ps.p), ps.limits(min(ps.room(m), ps.hostRoom)))
	for _, node := range result.Nodes {
		var taken []coverItem
		for i, n := range node.Placed {
			g := &c.groups[at[i]]
			taken = append(taken, g.items[:n]...)
			g.items = g.items[n:]
		}
		var ids []uuid.UUID
		var holds FleetCapacity
		for _, it := range taken {
			holds = holds.Plus(reservedShape(it.need))
			if !it.slot {
				ids = append(ids, it.id)
			}
		}
		action := ps.buy(m, node.Offer, ActionBuy, holds, ids, nil)
		for _, it := range taken {
			ps.provisioning(it, nil, ptr(action))
		}
	}
}

// shortReason says why market m's purchases fall short of shape: the
// pass's limits, or else the first exclusion whose removal lets an offer of
// the market cover it. An offer that covers it with every exclusion in
// place was held back by the quota room the pass's earlier purchases took.
func (ps *pass) shortReason(m ReserveMarket, shape FleetCapacity, reserve bool) MarketReason {
	switch {
	case ps.hostRoom <= 0 || reserve && ps.reserveRoom <= 0:
		return ReasonFleetLimit
	case ps.s.BatchWait > 0:
		return ReasonBatch
	case ps.room(m) <= 0:
		return ReasonActionCap
	}
	need := slotNeed(m, shape)
	offered := func(relax func(*OfferInputs)) bool {
		in := ps.s.Offers
		relax(&in)
		return slices.ContainsFunc(RankOffers(ps.p, need, reserve, in), func(o FleetOffer) bool {
			return o.Market == m.buyMarket() && o.Type.GPU == m.GPU
		})
	}
	switch {
	case offered(func(*OfferInputs) {}):
		return ReasonQuota
	case offered(func(in *OfferInputs) { in.Cooldowns = nil }):
		return ReasonCooldown
	case offered(func(in *OfferInputs) { in.Quotas = nil }):
		return ReasonQuota
	case offered(func(in *OfferInputs) { in.OwnerPays = true }):
		return ReasonMargin
	}
	return ReasonNoOffer
}

// idle records when each serving host without containers or pending work
// for it became idle.
func (ps *pass) idle() {
	for _, h := range ps.s.Hosts {
		if h.State != FleetServing || h.Containers > 0 || ps.claimed[h.ID] {
			continue
		}
		since := ps.s.Now
		if h.IdleSince != nil {
			since = *h.IdleSince
		}
		ps.plan.IdleSince[h.ID] = since
	}
}

// idleLong reports a host idle for the idle timeout.
func (ps *pass) idleLong(h FleetHost) bool {
	since, ok := ps.plan.IdleSince[h.ID]
	return ok && !h.Protected && ps.s.Now.Sub(since) >= ps.p.IdleTimeout
}

func (ps *pass) warmFree(v *marketView) FleetCapacity {
	return totalOf(ps.inMarket(v.m, serving), func(h *FleetHost) FleetCapacity { return h.free() })
}

func (ps *pass) warmPending(v *marketView) FleetCapacity {
	pending := totalOf(ps.inMarket(v.m, starting), func(h *FleetHost) FleetCapacity { return h.Usable })
	for _, n := range ps.planned {
		if n.market == v.m && !n.reserve {
			pending = pending.Plus(n.offer.Usable)
		}
	}
	return pending
}

// retain releases the idle hosts the market no longer needs once each has
// been idle for the timeout.
func (ps *pass) retain(v *marketView) {
	for _, h := range ps.leavers(v) {
		if ps.idleLong(*h) {
			ps.leave(v, h)
		}
	}
}

// leavers are market v's idle serving hosts that hold no warm slot,
// cheapest first.
func (ps *pass) leavers(v *marketView) []*FleetHost {
	out := ps.inMarket(v.m, func(h FleetHost) bool {
		_, idle := ps.plan.IdleSince[h.ID]
		return serving(h) && idle && !h.Protected && len(ps.holding[h.ID]) == 0
	})
	slices.SortStableFunc(out, ps.retention)
	return out
}

// leave returns a leaving host to the reserve when EC2 can stop it and the
// market's reserve falls short, holds nothing that fits the largest shape
// while the host does, or the host slept there before and the reserve
// holds less than its share of the peak, so a reserve woken for a burst
// waits stopped for the next. A host launched able to hibernate does where
// hibernates allows. Any other leaving host drains.
func (ps *pass) leave(v *marketView, h *FleetHost) {
	t, catalogued := ps.typeNamed(h.InstanceType)
	short := !ps.floor(v, HostID{}, FleetHost.reserve).Covers(v.stopped) || (h.Usable.Covers(v.largest) && !ps.holdsLargest(v, HostID{})) ||
		h.Slept && !ps.floor(v, HostID{}, FleetHost.reserve).Covers(v.peakShare)
	if ps.reserveRoom > 0 && catalogued && h.Stoppable && short {
		mode := ReserveStop
		if h.HibernationConfigured && hibernates(t) {
			mode = ReserveHibernate
		}
		h.State, h.ReserveMode, h.Current = FleetPreparing, ptr(mode), false
		// It runs until it stops, so it keeps its host room.
		ps.reserveRoom--
		ps.act(FleetAction{Kind: ActionReturnToReserve, Market: v.m, Host: ptr(h.ID), Mode: ptr(mode)})
		return
	}
	h.State = FleetDraining
	ps.act(FleetAction{Kind: ActionDrain, Market: v.m, Host: ptr(h.ID)})
}

// floor is what holds the market's stopped target: the reserves keep
// selects, less skip, retired and interrupted ones, which never resume.
func (ps *pass) floor(v *marketView, skip HostID, keep func(FleetHost) bool) FleetCapacity {
	return totalOf(ps.inMarket(v.m, func(h FleetHost) bool {
		return h.reserve() && !h.Protected && keep(h) && h.ID != skip && !v.retired[h.ID]
	}), func(h *FleetHost) FleetCapacity { return h.Usable })
}

// holdsLargest reports whether a reserve of the market other than skip, or
// one this pass buys, fits the market's largest shape.
func (ps *pass) holdsLargest(v *marketView, skip HostID) bool {
	if v.largest.Empty() {
		return true
	}
	for _, h := range ps.inMarket(v.m, FleetHost.reserve) {
		if h.ID != skip && !h.Protected && !v.retired[h.ID] && h.Usable.Covers(v.largest) {
			return true
		}
	}
	return slices.ContainsFunc(ps.planned, func(n plannedHost) bool {
		return n.market == v.m && n.reserve && n.offer.Usable.Covers(v.largest)
	})
}

// returning reports whether market v waits for hosts to return to the
// reserve before it buys what the reserve lacks, short in total and a host
// that fits item: while the reserve has lacked it for less than the return
// wait and any host could return, or while hosts due back soon cover it. A
// host could return when EC2 can stop it and it serves or starts. It is due
// back soon when it starts, became ready within the return wait,
// as a reserve resumed for a burst does, or idles holding no warm slot,
// slots a cheaper type will take over, or only a build's slot with room for
// the largest shape, which it holds warm until the slot lapses.
func (ps *pass) returning(v *marketView, short, item FleetCapacity) bool {
	leaving := ps.leavers(v)
	could := ps.inMarket(v.m, func(h FleetHost) bool {
		_, catalogued := ps.typeNamed(h.InstanceType)
		return (serving(h) || starting(h)) && h.Stoppable && !h.Protected && catalogued
	})
	if len(could) > 0 && ps.s.Now.Sub(*v.floorShortSince) < ps.p.ReturnWait {
		return true
	}
	soon := slices.DeleteFunc(could, func(h *FleetHost) bool {
		replaced, _ := ps.replacement(v, *h)
		_, idle := ps.plan.IdleSince[h.ID]
		holdsLargest := idle && !ps.holds(h.ID, slotFloor, slotLoad) && !v.largest.Empty() && h.Usable.Covers(v.largest)
		return !starting(*h) && ps.s.Now.Sub(h.PhaseAt) >= ps.p.ReturnWait && replaced == nil && !holdsLargest &&
			!slices.ContainsFunc(leaving, func(l *FleetHost) bool { return l.ID == h.ID })
	})
	return totalOf(soon, func(h *FleetHost) FleetCapacity { return h.Usable }).Covers(short) &&
		(item.Empty() || slices.ContainsFunc(soon, func(h *FleetHost) bool { return h.Usable.Covers(item) }))
}

// reserves keeps market v's stopped target, among it a reserve that fits
// the largest shape. What they lack is bought once no work waits in the
// market, the market no longer waits for hosts to return and containers
// have stopped arriving. With the target held, the reserves the market no
// longer needs retire.
func (ps *pass) reserves(v *marketView) {
	ps.retireUnresumable(v)
	short := v.stopped.Minus(ps.floor(v, HostID{}, FleetHost.reserve)).Clamp()
	var item FleetCapacity
	if !ps.holdsLargest(v, HostID{}) {
		item = v.largest
	}
	v.stoppedShort = short.Upper(item)
	if !v.stoppedShort.Empty() {
		since, ok := ps.s.FloorShortSince[v.m]
		if !ok {
			since = ps.s.Now
		}
		v.floorShortSince = &since
	}
	switch {
	case v.stoppedShort.Empty():
	case ps.waiting[v.m]:
		v.stoppedReason = ReasonDemand
	case ps.returning(v, short, item):
		v.stoppedReason = ReasonReturning
	case ps.batching():
		v.stoppedReason = ReasonBatch
	case v.loaded:
		// A share of load sets the target: the cheapest cover holds it, and
		// whichever of its hosts fits the largest shape holds that. In the
		// CPU Spot market each reserve holds the target up to the default
		// largest shape, the size that still hibernates, so a target growing
		// a little each pass buys a few large reserves that resume from
		// memory and cost about what small ones do to keep stopped.
		need := CoverNeed{Aggregate: short}
		if !item.Empty() {
			need = CoverNeed{Items: []CoverItem{{Shape: item, Count: 1}}, Aggregate: short.Minus(item).Clamp()}
		}
		var least FleetCapacity
		if v.m.spotCPU() {
			least = v.stopped.Lower(ps.p.LargestShape.Default)
		}
		result := ps.buyReserves(v, need, least, least)
		v.stoppedShort = need.Aggregate.Minus(result.Supplied).Clamp()
		if len(result.UnmetItems) > 0 {
			v.stoppedShort = short.Minus(result.Supplied).Clamp().Upper(item)
		}
		ps.reserveReason(v, item)
	default:
		// At the floor, the reserve that fits the largest shape hibernates
		// where it can; the rest of the target takes the cheapest room.
		var supplied FleetCapacity
		unmet := false
		if !item.Empty() {
			result := ps.buyReserves(v, CoverNeed{Items: []CoverItem{{Shape: item, Count: 1}}}, item, FleetCapacity{})
			for _, node := range result.Nodes {
				supplied = supplied.Plus(node.Offer.Usable)
			}
			unmet = len(result.UnmetItems) > 0
		}
		if rest := short.Minus(supplied).Clamp(); !rest.Empty() {
			supplied = supplied.Plus(ps.buyReserves(v, CoverNeed{Aggregate: rest}, FleetCapacity{}, FleetCapacity{}).Supplied)
		}
		v.stoppedShort = short.Minus(supplied).Clamp()
		if unmet {
			v.stoppedShort = v.stoppedShort.Upper(item)
		}
		ps.reserveReason(v, item)
	}
	if short.Empty() {
		ps.retire(v)
	}
}

// retireUnresumable retires the market's stopped reserves that never
// resume, so they hold none of the target: those that stopped after their
// reclaim notice, and those prepared for an older agent release.
func (ps *pass) retireUnresumable(v *marketView) {
	for _, h := range ps.inMarket(v.m, func(h FleetHost) bool { return h.resumable() && (h.Protected || !h.Current) }) {
		v.retired[h.ID] = true
		ps.reserveRoom++
		ps.act(FleetAction{Kind: ActionRetireReserve, Market: v.m, Host: ptr(h.ID)})
	}
}

// reserveReason settles market v's reserve shortfall after its purchases:
// held, or named by why the purchases fell short of item.
func (ps *pass) reserveReason(v *marketView, item FleetCapacity) {
	if v.stoppedShort.Empty() {
		v.floorShortSince = nil
		return
	}
	v.stoppedReason = ps.shortReason(v.m, item, true)
}

// buyReserves buys the cheapest reserves that cover need, ones that
// hibernate wherever one fits shape, each holding least where it can: what
// those cannot cover, under a quota or a cooldown, any offer covers.
func (ps *pass) buyReserves(v *marketView, need CoverNeed, shape, least FleetCapacity) CoverResult {
	offers := ps.marketOffers(v.m, true)
	if !shape.Empty() {
		offers = preferHibernating(offers, shape)
	}
	sized := slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool { return !o.Usable.Covers(least) })
	if least.Empty() || len(sized) == 0 {
		return ps.coverReserves(v, need, offers)
	}
	result := ps.coverReserves(v, need, sized)
	if result.Complete(need) {
		return result
	}
	rest := ps.coverReserves(v, CoverNeed{Items: result.UnmetItems, Aggregate: need.Aggregate.Minus(result.Supplied).Clamp()}, offers)
	return CoverResult{Nodes: append(result.Nodes, rest.Nodes...), UnmetItems: rest.UnmetItems, Supplied: result.Supplied.Plus(rest.Supplied)}
}

// coverReserves buys the cheapest reserves from offers that cover need.
func (ps *pass) coverReserves(v *marketView, need CoverNeed, offers []FleetOffer) CoverResult {
	result := Cover(offers, need, reserveCost(ps.p), ps.limits(min(ps.room(v.m), ps.reserveRoom, ps.hostRoom)))
	for _, node := range result.Nodes {
		holds := node.Offer.Usable.Lower(need.Aggregate)
		for i, n := range node.Placed {
			if n > 0 {
				holds = holds.Upper(need.Items[i].Shape)
			}
		}
		ps.buy(v.m, node.Offer, ActionBuyReserve, holds, nil, nil)
	}
	return result
}

// preferHibernating drops the offers that fit shape without hibernating
// while one that hibernates fits it, so the reserve that fits resumes from
// memory.
func preferHibernating(offers []FleetOffer, shape FleetCapacity) []FleetOffer {
	fits := func(o FleetOffer) bool { return o.Usable.Covers(shape) }
	if !slices.ContainsFunc(offers, func(o FleetOffer) bool { return o.Hibernate && fits(o) }) {
		return offers
	}
	return slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool { return !o.Hibernate && fits(o) })
}

// retire terminates reserves beyond the stopped target: ones the market
// cannot buy again first, then ones not ready, then the costliest to hold.
// It keeps the ready capacity the target needs, so a pending reserve never
// stands in for a ready one, and a reserve that fits the largest shape.
// Within the cost horizon of the market's peak load it keeps every reserve
// it would buy again that is ready or being prepared, so the reserves a
// burst bought or woke wait stopped for the next burst rather than
// retiring as its load falls.
func (ps *pass) retire(v *marketView) {
	// A peak older than the cost horizon is already empty.
	holding := !v.peak.Load.Empty()
	ready := FleetHost.resumable
	needReady := ps.floor(v, HostID{}, ready).Lower(v.stopped)
	growable := func(h *FleetHost) bool {
		_, ok := ps.typeNamed(h.InstanceType)
		return ok && !ps.cooled(*h)
	}
	candidates := ps.inMarket(v.m, func(h FleetHost) bool {
		return h.reserve() && !h.Protected && h.State != FleetStopping && !v.retired[h.ID]
	})
	slices.SortStableFunc(candidates, func(a, b *FleetHost) int {
		return cmp.Or(boolOrder(growable(a), growable(b)), boolOrder(ready(*a), ready(*b)),
			cmp.Compare(ps.stoppedMicros(*b), ps.stoppedMicros(*a)), cmp.Compare(b.Usable.CPUMillis, a.Usable.CPUMillis),
			cmp.Compare(b.Usable.MemoryBytes, a.Usable.MemoryBytes), strings.Compare(a.ID.String(), b.ID.String()))
	})
	for _, h := range candidates {
		if holding && growable(h) && (ready(*h) || h.State == FleetPreparing) {
			continue
		}
		if !ps.floor(v, h.ID, FleetHost.reserve).Covers(v.stopped) || !ps.floor(v, h.ID, ready).Covers(needReady) ||
			!ps.holdsLargest(v, h.ID) {
			continue
		}
		v.retired[h.ID] = true
		ps.reserveRoom++
		ps.act(FleetAction{Kind: ActionRetireReserve, Market: v.m, Host: ptr(h.ID)})
	}
}

// rightsize replaces the idle host whose cheaper type pays back most: the
// hourly saving over the cost horizon must exceed what the new host costs
// while it provisions, and the new type must hold the warm slots the host
// does. It replaces only an idle host a slot needs, and only while nothing
// else in the market grows, starts or leaves and no container arrives, so
// the warm slots converge on the cheapest type. The same type in another
// zone never replaces it, so a Spot price move between zones buys nothing.
func (ps *pass) rightsize(v *marketView) {
	moving := slices.ContainsFunc(ps.plan.Actions, func(a FleetAction) bool {
		return a.Market == v.m && a.Kind != ActionRetireReserve
	})
	if ps.waiting[v.m] || ps.s.BatchWait > 0 || moving || !ps.warmPending(v).Empty() || ps.room(v.m) <= 0 || ps.hostRoom <= 0 {
		return
	}
	var best *FleetOffer
	var replaced *FleetHost
	var bestPayback int64
	for _, h := range ps.inMarket(v.m, serving) {
		if !ps.idleLong(*h) {
			continue
		}
		if o, payback := ps.replacement(v, *h); o != nil && payback > bestPayback {
			best, replaced, bestPayback = o, h, payback
		}
	}
	if best != nil {
		holds := ps.held(replaced.ID)
		ps.buy(v.m, *best, ActionRightsize, holds, nil, ptr(replaced.ID))
	}
}

// replacement is the offer of another type that pays back most for
// replacing idle host h, with its payback, or nil: it holds the warm slots
// h does, and what it saves over the cost horizon, priced as purchases
// are, exceeds what it costs while it provisions. It does not wait for the
// idle timeout, so a host rightsize will replace counts as returning while
// it idles. A host whose replacement EC2 refused within the cost horizon
// has none, and neither has one holding a slot that follows the load: the
// payback assumes the slots last the horizon, and those leave with the
// load.
func (ps *pass) replacement(v *marketView, h FleetHost) (*FleetOffer, int64) {
	need := ps.held(h.ID)
	cost, known := ps.hostCost(h)
	_, idle := ps.plan.IdleSince[h.ID]
	refused := h.RightsizeRefusedAt != nil && ps.s.Now.Sub(*h.RightsizeRefusedAt) < ps.p.CostHorizon
	if !idle || h.Protected || refused || ps.holds(h.ID, slotLoad) || !known || need.Empty() {
		return nil, 0
	}
	var best *FleetOffer
	var bestPayback int64
	provision, serving := int64(ps.p.Provision/time.Second), servingCost(ps.p)
	for _, o := range ps.marketOffers(v.m, false) {
		payback := cost - serving(o) - o.HourlyMicros*provision
		if o.Type.Name != h.InstanceType && o.Usable.Covers(need) && payback > bestPayback {
			best, bestPayback = &o, payback
		}
	}
	return best, bestPayback
}

// report is the market's published plan. A market whose work waits on the
// fleet limit or for arrivals to settle says so; otherwise a warm shortfall
// names its reason, then a reserve shortfall its own.
func (ps *pass) report(v *marketView) MarketPlan {
	var reserve, ready FleetCapacity
	for _, h := range ps.inMarket(v.m, FleetHost.reserve) {
		if v.retired[h.ID] {
			continue
		}
		reserve = reserve.Plus(h.Usable)
		if h.resumable() {
			ready = ready.Plus(h.Usable)
		}
	}
	plan := MarketPlan{
		Market: v.m, Load: v.load, WarmTarget: totalOf(v.slots, func(s warmSlot) FleetCapacity { return s.shape }),
		WarmFree: ps.warmFree(v), WarmPending: ps.warmPending(v),
		StoppedTarget: v.stopped, ReserveReady: ready, ReservePending: reserve.Minus(ready).Clamp(),
		Shortfall: v.shortfall, StoppedShortfall: v.stoppedShort, FloorShortSince: v.floorShortSince, Peak: v.peak, States: ps.states(v.m),
	}
	switch {
	case ps.limited[v.m]:
		plan.Reason = ReasonFleetLimit
	case ps.batched[v.m]:
		plan.Reason = ReasonBatch
	case !plan.Shortfall.Empty():
		plan.Reason = v.warmReason
	case !plan.StoppedShortfall.Empty():
		plan.Reason = v.stoppedReason
	}
	return plan
}

// states totals the snapshot's hosts of a market by state.
func (ps *pass) states(m ReserveMarket) []FleetStateCapacity {
	var out []FleetStateCapacity
	for _, h := range ps.s.Hosts {
		if h.market() != m {
			continue
		}
		i := slices.IndexFunc(out, func(s FleetStateCapacity) bool { return s.State == h.State })
		if i < 0 {
			out = append(out, FleetStateCapacity{State: h.State})
			i = len(out) - 1
		}
		out[i].Machines++
		out[i].Capacity = out[i].Capacity.Plus(h.Usable)
		out[i].Allocated = out[i].Allocated.Plus(h.Load)
	}
	slices.SortFunc(out, func(a, b FleetStateCapacity) int { return strings.Compare(string(a.State), string(b.State)) })
	return out
}

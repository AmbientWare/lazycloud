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
	// Load is what its live containers reserve; Lent, on an on-demand
	// host, what those that could run on Spot reserve, which counts as the
	// load of the Spot market of its GPU model, or of CPU; Packed, on a GPU
	// host, what CPU work reserves, which counts as the load of the CPU
	// Spot market and which GPU work evicts.
	Load, Lent, Packed FleetCapacity
	// Containers counts the live containers that keep it up: on a GPU host,
	// its GPU work.
	Containers int
	// UsedDisks counts the disks its live containers attach.
	UsedDisks int
	// Protected is an interruption's source or replacement.
	Protected bool
	// StartRefused is set on a reserve whose start EC2 refused for capacity
	// within the capacity cooldown.
	StartRefused bool
	// Requested is set on a host EC2 has not launched yet, which its
	// launch's walk through refused pools may still move.
	Requested bool
	// Current is set on a reserve prepared on the target agent build.
	Current bool
	// UpdateDue is set on a host whose agent must move to the target
	// release; until it does, placement gives it no work.
	UpdateDue bool
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
	// Replaces is the idle host a rightsize bought this one to replace.
	Replaces *HostID
	// RightsizeRefusedAt is when EC2 last refused a launch to replace it.
	RightsizeRefusedAt *time.Time
	// StoppedAt is when a reserve's instance stopped; a stopping Spot
	// reserve has one while EC2 still holds its persistent request active.
	StoppedAt *time.Time
	// Slept is set on a host that has proved it can stop into the reserve:
	// a reserve, or one resumed from it.
	Slept bool
	// HandsBack is set on an on-demand host that hands Spot-tolerant work
	// back to Spot. Moves are what each of its containers that could move
	// to a Spot host needs; Stays is set while one of them could not.
	HandsBack bool
	Moves     []Requirement
	Stays     bool
}

func (h FleetHost) market() ReserveMarket { return reserveMarketOf(h.Market, h.GPU) }

// free is the room placement would give new work on the host.
func (h FleetHost) free() FleetCapacity {
	if h.UpdateDue {
		return FleetCapacity{}
	}
	return h.Usable.Minus(h.Load).Clamp()
}

// resumable is a stopped reserve.
func (h FleetHost) resumable() bool {
	return h.State == FleetStopped || h.State == FleetHibernateUnverified || h.State == FleetImageSaved
}

// reserve is a host held, or being made, a reserve.
func (h FleetHost) reserve() bool {
	return h.resumable() || h.State == FleetPreparing || h.State == FleetStopping
}

// held is a host its market keeps running or on its way to run: serving,
// starting or not launched yet.
func (h FleetHost) held() bool { return h.State == FleetServing || h.State == FleetStarting }

// capacity is the host as placement sees it, its disk slots those its
// type's data volume holds.
func (h FleetHost) capacity() HostCapacity {
	free := h.free()
	room := HostCapacity{
		Host: h.ID, Kind: KindPlatform, Provider: ProviderAWS, Region: h.Region, Zone: h.Zone, ZoneID: h.ZoneID,
		Market: h.Market, GPUType: h.GPU, GPUCount: h.Usable.GPUs,
		CPUMillis: h.Usable.CPUMillis, MemoryBytes: h.Usable.MemoryBytes,
		FreeCPUMillis: free.CPUMillis, FreeMemoryBytes: free.MemoryBytes, FreeGPUs: free.GPUs, HandsBack: h.HandsBack,
		Packed: h.Packed,
	}
	if t, ok := CatalogTypeNamed(h.InstanceType); ok && !h.UpdateDue {
		room.FreeDiskSlots = max(0, t.DiskSlots()-h.UsedDisks)
	}
	return room
}

// DemandGroup is pending platform containers with one requirement.
type DemandGroup struct {
	Need       Requirement
	Containers []PendingContainer
}

// PendingContainer is one pending container, when it was created and the
// host bought for it.
type PendingContainer struct {
	ID    uuid.UUID
	Since time.Time
	Host  *HostID
}

// FleetSnapshot is everything one planning pass reads.
type FleetSnapshot struct {
	Now     time.Time
	Hosts   []FleetHost
	Pending []DemandGroup
	// Recent is the largest shape each market's placed containers reserved
	// within the policy's LargestShape window, and RecentDisks the most
	// disks one of them attached; Arrived is what each CPU market's
	// containers placed within the arrival window reserve, less their
	// largest batch.
	Recent, Arrived map[ReserveMarket]FleetCapacity
	RecentDisks     map[ReserveMarket]int
	Offers                  OfferInputs
	// HostRoom is how many more hosts may run or start, reserves being
	// prepared among them; ReserveRoom how many more may be held stopped.
	HostRoom    int
	ReserveRoom int
	// BatchWait is how long the arrival batch stays open; zero once it has
	// closed.
	BatchWait time.Duration
	// Holds are each market's holds as the last pass left them.
	Holds map[ReserveMarket]MarketHolds
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
	// ActionCancel removes a requested host its market no longer keeps.
	ActionCancel FleetActionKind = "cancel"
	// ActionRightsize buys Offer to replace an idle Host; the host stays
	// until the replacement serves.
	ActionRightsize FleetActionKind = "rightsize"
	// ActionHandBack has an on-demand Host hand Spot-tolerant work back to
	// Spot; ActionLend has it lend that work room again.
	ActionHandBack FleetActionKind = "hand_back"
	ActionLend     FleetActionKind = "lend"
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
	// the containers the cover placed on it and the headroom or reserve
	// room it was bought for.
	Holds FleetCapacity
}

// ContainerWait is why a pending container waits for compute: nil Wait
// means ready room takes it. A provisioning wait names the host or the Buy
// action that will take it; any other names why none can yet.
type ContainerWait struct {
	Container uuid.UUID
	Wait      *CapacityWait
	Host      *HostID
	Action    *int
}

// MarketPlan is one market's targets and measures.
type MarketPlan struct {
	Market ReserveMarket
	// Load is what the market's containers hold now: running and pending.
	Load FleetCapacity
	// WarmTarget is the room the market's running target keeps beyond its
	// load; WarmFree and WarmPending what its serving hosts have free and
	// its starting ones hold.
	WarmTarget, WarmFree, WarmPending FleetCapacity
	StoppedTarget                     FleetCapacity
	ReserveReady, ReservePending      FleetCapacity
	// Shortfall is what the running target lacks after the pass, and
	// StoppedShortfall what the reserves lack.
	Shortfall, StoppedShortfall FleetCapacity
	// Holds carry the market's targets to the next pass.
	Holds  MarketHolds
	Reason CapacityWait
	States []FleetStateCapacity
}

// FleetPlan is one pass's decisions.
type FleetPlan struct {
	Markets []MarketPlan
	Actions []FleetAction
	Waits   []ContainerWait
	// BatchWait is how long the purchases the pass held wait for arrivals
	// to settle; zero when it held none.
	BatchWait time.Duration
}

// PlanFleet decides one pass over a snapshot. On-demand hosts first decide
// whether they lend to Spot-tolerant work. Pending containers then take
// ready room, as placement would, then hosts bought or starting for them,
// then reserves resumed for them, then purchases, which wait while
// containers keep arriving. Each market then keeps the hosts its running
// target needs, in a fixed order, grows by what they lack, and sends the
// rest to the reserve while it falls short, or away; keeps the reserves
// its stopped target needs, buys what they lack and retires the rest; and
// rightsizes an idle host a cheaper type could replace.
func PlanFleet(p Policy, s FleetSnapshot) FleetPlan {
	ps := &pass{
		p: p, s: s, hosts: slices.Clone(s.Hosts),
		used: map[ReserveMarket]int{}, waiting: map[ReserveMarket]bool{},
		limited: map[ReserveMarket]bool{}, batched: map[ReserveMarket]bool{},
		hostRoom: s.HostRoom, reserveRoom: s.ReserveRoom, offers: map[string]ranking{}, byMarket: map[ReserveMarket]*marketView{},
		claimed: map[HostID]bool{}, quotaUsed: QuotaUse(s.Hosts, s.Offers.Catalog), spot: freshSpot(p, s.Offers.Spot, s.Now),
		classReads: map[string]int64{}, spotOffers: map[spotNeed]bool{},
	}
	ps.s.Offers.QuotaUsed = maps.Clone(ps.quotaUsed)
	for _, g := range s.Pending {
		if g.Need.Read != nil {
			_, key := ps.classKey(g.Need)
			ps.classReads[key] = max(ps.classReads[key], g.Need.Read.Bytes)
		}
	}
	ps.handBack()
	items := ps.pendingItems()
	views := ps.views(items)
	ps.cover(items)
	for _, v := range views {
		idle := ps.keep(v)
		ps.reserves(v)
		ps.rightsize(v, idle)
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
	// waiting are markets with pending work left without a host, limited
	// those whose work the fleet limit holds back and batched those whose
	// purchases wait for arrivals to settle.
	waiting, limited, batched map[ReserveMarket]bool
	hostRoom, reserveRoom     int
	offers                    map[string]ranking
	// classReads is the largest read pending work of each demand class
	// makes, by class key.
	classReads map[string]int64
	// spotOffers holds spotOffered's answers.
	spotOffers map[spotNeed]bool
	byMarket   map[ReserveMarket]*marketView
	// claimed are hosts pending work takes room on, serving or starting.
	claimed map[HostID]bool
	// quotaUsed is what running hosts and this pass's starts count
	// against each vCPU quota.
	quotaUsed map[QuotaKey]int64
	// spot is the freshest quote of each Spot pool.
	spot map[spotPool]SpotQuote
}

// plannedHost is a host the pass buys, in the market it will serve, and
// what it was bought to hold.
type plannedHost struct {
	market  ReserveMarket
	offer   FleetOffer
	reserve bool
	holds   FleetCapacity
	// work is set on a purchase for pending containers.
	work bool
}

// marketView is one market during the pass.
type marketView struct {
	m ReserveMarket
	MarketTarget
	retired map[HostID]bool
	supply  reserveSupply
	// shortfall and stoppedShort are what the pass could not cover, and
	// warmReason and stoppedReason why.
	shortfall, stoppedShort   FleetCapacity
	warmReason, stoppedReason CapacityWait
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

// coverItem is one pending container during the cover.
type coverItem struct {
	id     uuid.UUID
	since  time.Time
	need   Requirement
	market ReserveMarket
	bought *HostID
	wait   int
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
			items = append(items, coverItem{id: c.ID, since: c.Since, need: g.Need, market: m, bought: c.Host, wait: len(ps.plan.Waits) - 1})
		}
	}
	slices.SortStableFunc(items, func(a, b coverItem) int {
		return cmp.Or(boolOrder(a.bought == nil, b.bought == nil), boolOrder(a.need.Preemptible, b.need.Preemptible), size(b.need)-size(a.need))
	})
	return items
}

// views sets every market's target from what its hosts' containers
// reserve, what its pending containers ask for, what arrived in it and
// the largest shape its work reserved.
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
		in := targetInputs{load: ps.marketLoad(m), arrived: ps.s.Arrived[m], largest: ps.s.Recent[m], disks: ps.s.RecentDisks[m], carried: ps.s.Holds[m]}
		for _, it := range items {
			if it.market == m {
				in.load = in.load.Plus(reservedShape(it.need))
				in.largest, in.disks = in.largest.Upper(reservedShape(it.need)), max(in.disks, it.need.Disks)
			}
		}
		v := &marketView{m: m, MarketTarget: ps.p.target(m, in, ps.s.Now), retired: map[HostID]bool{}}
		ps.byMarket[m] = v
		views = append(views, v)
	}
	for _, v := range views {
		v.supply = ps.supply(v)
	}
	return views
}

// marketLoad is what market m's work holds on running hosts: its hosts'
// containers, less what Spot-tolerant work borrows on them and CPU work
// packs on them, for a Spot market what its work borrows on on-demand
// hosts of its GPU model, or of CPU, and for the CPU Spot market what its
// work packs on GPU hosts.
func (ps *pass) marketLoad(m ReserveMarket) FleetCapacity {
	var load FleetCapacity
	for _, h := range ps.hosts {
		switch {
		case h.market() == m:
			load = load.Plus(h.Load.Minus(h.Lent).Minus(h.Packed))
		case m == reserveMarketOf(MarketSpot, h.GPU):
			load = load.Plus(h.Lent)
		}
		if m == (ReserveMarket{Preemptible: true}) {
			load = load.Plus(h.Packed)
		}
	}
	return load
}

// marketNeed is what work of market m asks of an offer.
func marketNeed(m ReserveMarket) Requirement {
	need := Requirement{Preemptible: m.Preemptible}
	if m.GPU != "" {
		need.GPUs = []string{m.GPU}
	}
	return need
}

// shapeNeed is room of shape in market m as a requirement.
func shapeNeed(m ReserveMarket, shape FleetCapacity) Requirement {
	need := marketNeed(m)
	need.CPUMillis, need.MemoryBytes, need.GPUCount = shape.CPUMillis, shape.MemoryBytes, shape.GPUs
	return need
}

// needOf is class need asking for shape.
func needOf(class Requirement, shape FleetCapacity) Requirement {
	class.CPUMillis, class.MemoryBytes, class.GPUCount = shape.CPUMillis, shape.MemoryBytes, shape.GPUs
	return class
}

// reservedShape is what a requirement reserves on a host.
func reservedShape(r Requirement) FleetCapacity {
	return FleetCapacity{CPUMillis: r.CPUMillis, MemoryBytes: r.MemoryBytes, GPUs: r.GPUsNeeded()}
}

// marketOffers are the offers a market's own growth buys from, priced with
// the read its recent work makes.
func (ps *pass) marketOffers(m ReserveMarket, reserve bool) []FleetOffer {
	return ps.marketRanking(m, reserve).offers
}

// marketRanking ranks market m's offers once per pass.
func (ps *pass) marketRanking(m ReserveMarket, reserve bool) ranking {
	key := m.String() + "/" + boolKey(reserve)
	if r, ok := ps.offers[key]; ok {
		return r
	}
	need := marketNeed(m)
	need.Read = ps.s.Offers.marketRead(m)
	r := ranking{refs: sizeRefs(ps.p, need, reserve, ps.s.Offers)}
	if r.refs != nil {
		r.soon = RankOffers(ps.p, need, reserve, availableSoon(ps.p, ps.s.Offers))
	}
	r.offers = slices.DeleteFunc(RankOffers(ps.p, need, reserve, ps.s.Offers), func(o FleetOffer) bool {
		return o.Market != m.buyMarket() || o.Type.GPU != m.GPU
	})
	ps.offers[key] = r
	return r
}

// ranking is the offers for one need, best first, and what the size rule
// sizes them against: the sizes of GPU offers and those available soon.
type ranking struct{ offers, refs, soon []FleetOffer }

// misplaced reports a region outside the one market v's starts read from,
// while the market's cheapest reserve that holds its largest shape lies in
// the read region and buying it costs less than a start's read from the
// region saves. A reserve there holds none of the stopped target, so the
// market buys one in the read region and retires it once that is ready; a
// host there does not return to the reserve.
func (ps *pass) misplaced(v *marketView, region string) bool {
	r := ps.s.Offers.marketRead(v.m)
	if r == nil || region == r.Region {
		return false
	}
	offers := preferHibernating(ps.marketOffers(v.m, true), v.Largest, v.LargestDisks)
	i := slices.IndexFunc(offers, func(o FleetOffer) bool { return v.fits(o.Usable, o.Type) })
	saved := readMicros(ps.p, r, region) * int64(ps.p.CostHorizon/time.Second)
	return i >= 0 && offers[i].Region == r.Region && offers[i].HourlyMicros*int64(ps.p.Provision/time.Second) < saved
}

// placed is a reserve of market v that holds its stopped target: one not
// stale, not launched yet wherever its walk points, and a launched one only
// where it is not misplaced.
func (ps *pass) placed(v *marketView) func(FleetHost) bool {
	return func(h FleetHost) bool { return h.reserve() && !stale(h) && (h.Requested || !ps.misplaced(v, h.Region)) }
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

// stoppedMicros is what a host costs stopped: its EBS volumes.
func (ps *pass) stoppedMicros(h FleetHost) int64 {
	t, ok := ps.typeNamed(h.InstanceType)
	if !ok {
		return 0
	}
	return t.volumesMicros(h.Region, h.ReserveMode != nil && *h.ReserveMode == ReserveHibernate, h.ReserveMode != nil)
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

// limits bounds a cover to nodes and the quota room left, each node sized
// for what it holds against r.
func (ps *pass) limits(nodes int, r ranking) CoverLimits {
	return CoverLimits{Nodes: nodes, VCPUs: quotaRoom(ps.s.Offers.Quotas, ps.quotaUsed), Sized: func(o FleetOffer, holds FleetCapacity, disks int) bool {
		return ps.p.sized(o, holds, disks, r.refs, r.soon)
	}}
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
	case ActionReturnToReserve, ActionDrain, ActionRetireReserve, ActionCancel, ActionHandBack, ActionLend:
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
	market := reserveMarketOf(o.Market, o.Type.GPU)
	ps.planned = append(ps.planned, plannedHost{market: market, offer: o, reserve: reserve, holds: holds, work: len(containers) > 0})
	ps.start(o.Region, o.Type.Name, o.Market)
	return ps.act(FleetAction{Kind: kind, Market: m, Host: replaces, Offer: &o, Mode: mode, Containers: containers, Holds: holds})
}

func (ps *pass) resume(m ReserveMarket, h *FleetHost, containers []uuid.UUID) {
	// A stopping reserve holds its host room and quota already.
	if h.State != FleetStopping {
		ps.hostRoom--
		ps.start(h.Region, h.InstanceType, h.Market)
	}
	h.State = FleetStarting
	ps.reserveRoom++
	ps.act(FleetAction{Kind: ActionResume, Market: m, Host: ptr(h.ID), Containers: containers})
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

// take reserves need on the bin ChooseHost picks, as placement would, and
// returns its index, or -1. rooms mirror the bins' room.
func take(bins []bin, rooms []HostCapacity, need Requirement, floor FleetCapacity) int {
	i := ChooseHost(rooms, need, floor)
	if i >= 0 {
		bins[i].room.Reserve(need)
		rooms[i] = bins[i].room
		bins[i].host.Load = bins[i].host.Load.Plus(reservedShape(need))
	}
	return i
}

// binRooms are the bins' room.
func binRooms(bins []bin) []HostCapacity {
	rooms := make([]HostCapacity, len(bins))
	for i, b := range bins {
		rooms[i] = b.room
	}
	return rooms
}

// cover places pending containers in one pass: each takes room on the
// ready host placement would give it, then the host bought for it, then a
// starting host. Ready reserves resume for what is left, and purchases
// cover the rest. A requested host no pending container takes stays only
// while its market keeps it.
func (ps *pass) cover(items []coverItem) {
	var ready, coming []bin
	for i := range ps.hosts {
		h := &ps.hosts[i]
		switch h.State {
		case FleetServing:
			ready = append(ready, bin{host: h, room: h.capacity()})
		case FleetStarting:
			if !ps.stalled(*h) {
				coming = append(coming, bin{host: h, room: h.capacity()})
			}
		case FleetDraining, FleetUnavailable, FleetTerminating, FleetPreparing, FleetStopping, FleetStopped,
			FleetHibernateUnverified, FleetImageSaved:
		}
	}
	readyRooms, comingRooms := binRooms(ready), binRooms(coming)
	floor := ps.p.OnDemand.Floor
	var left []coverItem
	for _, it := range items {
		if it.need.Machine != "" || it.need.Connection != nil {
			continue
		}
		if i := take(ready, readyRooms, it.need, floor); i >= 0 {
			ps.claimed[ready[i].host.ID] = true
			continue
		}
		if it.bought != nil {
			if i := slices.IndexFunc(coming, func(b bin) bool { return b.host.ID == *it.bought }); i >= 0 {
				coming[i].room.Reserve(it.need)
				comingRooms[i] = coming[i].room
				coming[i].host.Load = coming[i].host.Load.Plus(reservedShape(it.need))
				ps.claimed[coming[i].host.ID] = true
				ps.provisioning(it, ptr(coming[i].host.ID), nil)
				continue
			}
			// A stale reserve resumed for it serves once its agent updates.
			if i := slices.IndexFunc(ready, func(b bin) bool { return b.host.ID == *it.bought && b.host.UpdateDue }); i >= 0 {
				ps.claimed[ready[i].host.ID] = true
				ps.provisioning(it, ptr(ready[i].host.ID), nil)
				continue
			}
		}
		// CPU work packs only into a GPU host that serves, so it never
		// waits on one booting.
		booting := it.need
		booting.Packs = false
		if i := take(coming, comingRooms, booting, floor); i >= 0 {
			ps.claimed[coming[i].host.ID] = true
			ps.provisioning(it, ptr(coming[i].host.ID), nil)
			continue
		}
		left = append(left, it)
	}
	ps.buyFor(ps.resumeFor(left))
}

// provisioning records that a pending container waits for a host.
func (ps *pass) provisioning(it coverItem, host *HostID, action *int) {
	w := &ps.plan.Waits[it.wait]
	w.Wait, w.Host, w.Action = ptr(WaitProvisioning), host, action
}

// waits records why a pending container waits with no host to take it.
func (ps *pass) waits(it coverItem, wait CapacityWait) {
	ps.plan.Waits[it.wait].Wait = ptr(wait)
}

// demandMarket is the market a container's demand belongs to: Spot or
// on-demand as it tolerates, of CPU or of the GPU model it would buy.
func (ps *pass) demandMarket(need Requirement) ReserveMarket {
	m := ReserveMarket{Preemptible: need.Preemptible}
	if need.GPUsNeeded() == 0 {
		return m
	}
	m.GPU = GPUAny
	if models := ps.gpuModels(need, ps.classOffers(need).offers); len(models) > 0 {
		m.GPU = models[0]
	} else if i := slices.IndexFunc(need.GPUs, func(model string) bool { return model != GPUAny }); i >= 0 {
		m.GPU = need.GPUs[i]
	}
	return m
}

// spotNeed is what spotOffered depends on in a requirement.
type spotNeed struct {
	class string
	shape FleetCapacity
	disks int
}

// spotOffered reports whether a Spot offer of a GPU model need may buy can
// host it now: none while every such pool cools after refusals or its
// quota is full. The answer holds for the pass.
func (ps *pass) spotOffered(need Requirement) bool {
	_, class := ps.classKey(need)
	key := spotNeed{class: class, shape: reservedShape(need), disks: need.Disks}
	if offered, ok := ps.spotOffers[key]; ok {
		return offered
	}
	r := ps.classOffers(need)
	models := ps.gpuModels(need, r.offers)
	offered := slices.ContainsFunc(r.offers, func(o FleetOffer) bool {
		return o.Market == MarketSpot && (key.shape.GPUs == 0 || slices.Contains(models, o.Type.GPU)) && o.Usable.Covers(key.shape) &&
			o.Type.DiskSlots() >= need.Disks && ps.p.sized(o, key.shape, need.Disks, r.refs, r.soon)
	})
	ps.spotOffers[key] = offered
	return offered
}

// handBack decides which serving on-demand hosts lend room to
// Spot-tolerant work. A host whose work could all move starts handing it
// back once a Spot offer can host each of its containers and no Spot pool
// of its region and kind cools after a refusal; it lends again once Spot
// cannot host one of them. A host that runs work that cannot move stays up
// for it, so its spare room costs nothing and it lends.
func (ps *pass) handBack() {
	for i := range ps.hosts {
		h := &ps.hosts[i]
		if h.Market != MarketOnDemand || h.State != FleetServing {
			continue
		}
		offered := !slices.ContainsFunc(h.Moves, func(need Requirement) bool { return !ps.spotOffered(need) })
		back := offered && !h.Stays && (h.HandsBack || len(h.Moves) > 0 && !ps.spotCooling(*h))
		if back == h.HandsBack {
			continue
		}
		h.HandsBack = back
		kind := ActionLend
		if back {
			kind = ActionHandBack
		}
		ps.act(FleetAction{Kind: kind, Market: h.market(), Host: ptr(h.ID)})
	}
}

// spotCooling reports whether a Spot pool of host h's region and GPU model,
// or of CPU, cools after a refusal.
func (ps *pass) spotCooling(h FleetHost) bool {
	return slices.ContainsFunc(ps.s.Offers.Cooldowns, func(c OfferCooldown) bool {
		t, ok := ps.typeNamed(c.InstanceType)
		return c.Market == MarketSpot && c.Region == h.Region && c.Until.After(ps.s.Now) && ok && t.GPU == h.GPU
	})
}

// classKey is what offers depend on in a requirement, and its key. Work
// that reads from a region's buckets is priced with the largest read its
// class makes there, so it shares purchases with the rest of the class.
func (ps *pass) classKey(need Requirement) (Requirement, string) {
	c := Requirement{Region: need.Region, Zone: need.Zone, Preemptible: need.Preemptible, GPUs: need.GPUs}
	key := "demand/" + c.Region + "/" + c.Zone + "/" + boolKey(c.Preemptible) + "/" + strings.Join(c.GPUs, ",")
	if need.Read != nil {
		key += "/" + need.Read.Region
		c.Read = &VolumeRead{Region: need.Read.Region, Bytes: ps.classReads[key]}
	}
	return c, key
}

// classOffers ranks the offers for work of need's placement, GPUs and read
// region, once per pass.
func (ps *pass) classOffers(need Requirement) ranking {
	c, key := ps.classKey(need)
	if r, ok := ps.offers[key]; ok {
		return r
	}
	r := ranking{offers: RankOffers(ps.p, c, false, ps.s.Offers), refs: sizeRefs(ps.p, c, false, ps.s.Offers)}
	if r.refs != nil {
		r.soon = RankOffers(ps.p, c, false, availableSoon(ps.p, ps.s.Offers))
	}
	ps.offers[key] = r
	return r
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

// resumeFor resumes the best ready reserve for pending containers until
// none takes any, and returns what is left. A resume waits for no batch:
// it is faster than any purchase.
func (ps *pass) resumeFor(items []coverItem) []coverItem {
	for len(items) > 0 && ps.hostRoom > 0 {
		h, taken := ps.bestReserve(items)
		if h == nil {
			break
		}
		ids := make([]uuid.UUID, len(taken))
		for n, i := range taken {
			ids[n] = items[i].id
		}
		ps.resume(items[taken[0]].market, h, ids)
		ps.claimed[h.ID] = true
		var rest []coverItem
		for n, it := range items {
			if slices.Contains(taken, n) {
				h.Load = h.Load.Plus(reservedShape(it.need))
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
// index, it takes first fit: one of its first item's market first, then
// in resume order, all it is for being what its market lacks of the
// running target, the work and the floor among it. Spot work
// takes an on-demand reserve only while no Spot offer can host it, and a
// CPU one only while it is lendable.
func (ps *pass) bestReserve(items []coverItem) (*FleetHost, []int) {
	var best *FleetHost
	var bestTaken []int
	all := func(h *FleetHost) bool {
		v := ps.byMarket[h.market()]
		return h.Usable.Covers(v.Running.Minus(ps.held(v)))
	}
	spotWork := slices.ContainsFunc(items, func(it coverItem) bool { return it.need.Preemptible })
	for i := range ps.hosts {
		h := &ps.hosts[i]
		if !ps.ready(*h, true) {
			continue
		}
		onDemand := h.Market == MarketOnDemand
		lendsCPU := spotWork && onDemand && h.GPU == "" && ps.lendable(*h)
		lends := func(need Requirement) bool { return !onDemand || (lendsCPU || h.GPU != "") && !ps.spotOffered(need) }
		room := h.capacity()
		var taken []int
		for n, it := range items {
			if ps.room(it.market) <= 0 || !room.Fits(it.need) || it.need.Preemptible && !lends(it.need) {
				continue
			}
			room.Reserve(it.need)
			taken = append(taken, n)
		}
		if len(taken) == 0 {
			continue
		}
		if best == nil || cmp.Or(boolOrder(h.market() != items[taken[0]].market, best.market() != items[bestTaken[0]].market),
			ps.resumeOrder(h, best, all)) < 0 {
			best, bestTaken = h, taken
		}
	}
	return best, bestTaken
}

// ready reports whether reserve h can resume now: one whose start EC2
// refused waits out the capacity cooldown; one stopped on the target agent
// build resumes for anything; for work, one stopped on an older build or
// stopping resumes when it serves sooner than a purchase. A stopping
// reserve's quota is counted already.
func (ps *pass) ready(h FleetHost, work bool) bool {
	stopping := h.State == FleetStopping && h.StoppedAt != nil && !ps.stalled(h)
	startable := h.resumable() && ps.startable(h) || stopping
	if h.Protected || h.StartRefused || !startable {
		return false
	}
	if h.resumable() && h.Current {
		return true
	}
	st := ps.p.Starts
	launch, serves := st.Launch, st.Resume
	if h.GPU != "" {
		launch, serves = st.GPULaunch, st.GPUResume
	}
	if stopping {
		serves += max(0, h.StoppedAt.Add(st.SpotDisable).Sub(ps.s.Now))
	}
	if !h.Current {
		serves += st.Update
	}
	return work && serves < launch
}

// stale reports a reserve stopped on an agent build other than the
// target's: it updates its agent before it serves, so it holds none of the
// stopped target and retires once a reserve on the target replaces it.
func stale(h FleetHost) bool {
	return (h.resumable() || h.State == FleetStopping) && !h.Current
}

// stalled reports a resumed Spot reserve whose start still waits for EC2
// to disable its request past SpotDisableMax: work stops waiting for it.
func (ps *pass) stalled(h FleetHost) bool {
	return h.StoppedAt != nil && ps.s.Now.Sub(*h.StoppedAt) > ps.p.Starts.SpotDisableMax
}

// leavesLarge reports whether the reserve supply of h's market without h
// still holds one that fits its largest shape, where h fits it.
func (ps *pass) leavesLarge(h FleetHost) bool {
	v := ps.byMarket[h.market()]
	if v.Largest.Empty() || !ps.fitsLargest(v, h) {
		return true
	}
	_, large := v.supply.without(ps, v, h.ID)
	return large
}

// fitsLargest reports whether host h fits market v's largest shape, with a
// disk slot for each of its disks.
func (ps *pass) fitsLargest(v *marketView, h FleetHost) bool {
	t, _ := ps.typeNamed(h.InstanceType)
	return v.fits(h.Usable, t)
}

// fits reports whether a host of type t with usable room fits the
// market's largest shape and its disks.
func (v *marketView) fits(usable FleetCapacity, t CatalogType) bool {
	return usable.Covers(v.Largest) && t.DiskSlots() >= v.LargestDisks
}

// retention orders hosts by what keeping them serving costs, cheapest and
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
// address, its expected transfer and its market's read. It carries no
// placement penalty: the penalty predicts whether a launch fills, and h
// already runs. Without a current price it is the price h recorded with
// its transfer and read; false when neither is known.
func (ps *pass) hostCost(h FleetHost) (int64, bool) {
	t, catalogued := ps.typeNamed(h.InstanceType)
	var o FleetOffer
	if catalogued {
		o.TransferMicros = transferMicros(ps.s.Offers.StorageRegion, h.Region, t)
	}
	o.ReadMicros = readMicros(ps.p, ps.s.Offers.marketRead(h.market()), h.Region)
	compute, priced := t.OnDemandMicros(h.Region)
	if h.Market == MarketSpot {
		q, quoted := ps.spot[spotPool{h.Region, h.ZoneID, h.InstanceType}]
		compute, priced = q.HourlyMicros, quoted
	}
	switch {
	case catalogued && priced:
		o.HourlyMicros = compute + t.volumesMicros(h.Region, h.HibernationConfigured, false) + ratesIn(h.Region).ipv4Hour
	case h.HourlyMicros != nil:
		o.HourlyMicros = *h.HourlyMicros
	default:
		return 0, false
	}
	return servingCost(ps.p)(o), true
}

// resumePays reports whether resuming reserve h for room of need costs no
// more over the cost horizon than the cheapest purchase that covers it.
// The resume is priced without the root it pays stopped as well, the
// purchase with what it runs while it provisions. With no purchase that
// covers it, a resume is the only way.
func (ps *pass) resumePays(h *FleetHost, need CoverNeed) bool {
	m := h.market()
	r := ps.marketRanking(m, false)
	result := Cover(r.offers, need, servingCost(ps.p), ps.limits(min(ps.room(m), ps.hostRoom), r))
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

// lendable reports whether Spot work may resume on-demand CPU reserve h:
// the ready on-demand reserves left without it still meet their stopped
// target and hold one that fits the largest shape.
func (ps *pass) lendable(h FleetHost) bool {
	v := ps.byMarket[h.market()]
	ready := totalOf(ps.inMarket(v.m, func(r FleetHost) bool {
		return r.resumable() && !r.Protected && r.ID != h.ID && !v.retired[r.ID]
	}), func(r *FleetHost) FleetCapacity { return r.Usable })
	return ready.Covers(v.Stopped) && ps.leavesLarge(h)
}

// demandClass is pending containers that can share offers, by shape.
type demandClass struct {
	need   Requirement
	key    string
	market ReserveMarket
	groups []itemGroup
}

// itemGroup is a class's containers of one shape and disk count.
type itemGroup struct {
	shape FleetCapacity
	disks int
	items []coverItem
}

// coverNeed is what is left of the class's groups, of those only keeps
// when it is set, and the index of each need item's group.
func coverNeed(c *demandClass, only func(coverItem) bool) (CoverNeed, []int) {
	var need CoverNeed
	var at []int
	for i, g := range c.groups {
		if len(g.items) == 0 || only != nil && !only(g.items[0]) {
			continue
		}
		need.Items = append(need.Items, CoverItem{Shape: g.shape, Disks: g.disks, Count: len(g.items)})
		at = append(at, i)
	}
	return need, at
}

// buyFor covers what no running host or resumed reserve holds with new
// hosts, per class of containers that share offers, unless containers are
// still arriving or the fleet limit is reached, and records why each
// container left waits.
func (ps *pass) buyFor(items []coverItem) {
	var classes []*demandClass
	for _, it := range items {
		need, key := ps.classKey(it.need)
		n := slices.IndexFunc(classes, func(c *demandClass) bool { return c.key == key })
		if n < 0 {
			classes = append(classes, &demandClass{need: need, key: key, market: it.market})
			n = len(classes) - 1
		}
		c := classes[n]
		shape := reservedShape(it.need)
		i := slices.IndexFunc(c.groups, func(g itemGroup) bool { return g.shape == shape && g.disks == it.need.Disks })
		if i < 0 {
			c.groups = append(c.groups, itemGroup{shape: shape, disks: it.need.Disks})
			i = len(c.groups) - 1
		}
		c.groups[i].items = append(c.groups[i].items, it)
	}
	for _, c := range classes {
		held := ps.hostRoom > 0 && ps.batching()
		if ps.hostRoom > 0 && !held {
			ps.buyClass(c, ps.classOffers(c.need))
		}
		for _, g := range c.groups {
			var short *CapacityWait
			for _, it := range g.items {
				ps.waiting[c.market] = true
				switch {
				case ps.hostRoom <= 0:
					ps.waits(it, WaitLimit)
					ps.limited[c.market] = true
				case held:
					ps.waits(it, WaitBatch)
					ps.batched[c.market] = true
				default:
					if short == nil {
						short = ptr(ps.shortReason(c.market, needOf(c.need, g.shape), false))
					}
					ps.waits(it, *short)
				}
			}
		}
	}
}

// buyClass covers what is left of class c from offers. GPU work buys its
// models in its preference order: Spot-tolerant work on Spot, on-demand
// what no Spot offer of any model can host, and, once it has waited the
// Spot wait, an on-demand host of its own whatever the margin.
func (ps *pass) buyClass(c *demandClass, r ranking) {
	m, offers := c.market, r.offers
	if m.GPU == "" {
		ps.coverClass(c, m, offers, r, nil)
		return
	}
	models := ps.gpuModels(c.need, offers)
	buy := func(market Market, offers []FleetOffer, only func(coverItem) bool) {
		for _, model := range models {
			ps.coverClass(c, ReserveMarket{Preemptible: m.Preemptible, GPU: model}, slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool {
				return o.Type.GPU != model || o.Market != market
			}), r, only)
		}
	}
	if !m.Preemptible {
		buy(MarketOnDemand, offers, nil)
		return
	}
	buy(MarketSpot, offers, nil)
	buy(MarketOnDemand, offers, func(it coverItem) bool { return !ps.spotOffered(it.need) })
	waited := func(it coverItem) bool { return ps.s.Now.Sub(it.since) >= ps.p.SpotWait && !ps.spotOffered(it.need) }
	if !slices.ContainsFunc(c.groups, func(g itemGroup) bool { return slices.ContainsFunc(g.items, waited) }) {
		return
	}
	in := ps.s.Offers
	in.WaiveMargin = true
	atLoss := ranking{offers: RankOffers(ps.p, c.need, false, in), refs: sizeRefs(ps.p, c.need, false, in)}
	atLoss.soon = RankOffers(ps.p, c.need, false, availableSoon(ps.p, in))
	for i := range c.groups {
		g := &c.groups[i]
		for _, it := range slices.Clone(g.items) {
			if !waited(it) {
				continue
			}
			alone := &demandClass{need: c.need, key: c.key, market: m, groups: []itemGroup{{shape: g.shape, disks: g.disks, items: []coverItem{it}}}}
			for _, model := range ps.gpuModels(c.need, atLoss.offers) {
				ps.coverClass(alone, ReserveMarket{Preemptible: true, GPU: model}, slices.DeleteFunc(slices.Clone(atLoss.offers), func(o FleetOffer) bool {
					return o.Type.GPU != model || o.Market != MarketOnDemand
				}), atLoss, nil)
			}
			if len(alone.groups[0].items) == 0 {
				g.items = slices.DeleteFunc(g.items, func(o coverItem) bool { return o.id == it.id })
			}
		}
	}
}

// coverClass buys from offers the cheapest cover of what is left of class
// c's groups, of those only keeps when it is set, each host sized against
// r for what it holds.
func (ps *pass) coverClass(c *demandClass, m ReserveMarket, offers []FleetOffer, r ranking, only func(coverItem) bool) {
	need, at := coverNeed(c, only)
	if len(at) == 0 {
		return
	}
	result := Cover(offers, need, servingCost(ps.p), ps.limits(min(ps.room(m), ps.hostRoom), r))
	for _, node := range result.Nodes {
		var taken []coverItem
		for i, n := range node.Placed {
			g := &c.groups[at[i]]
			taken = append(taken, g.items[:n]...)
			g.items = g.items[n:]
		}
		ids := make([]uuid.UUID, len(taken))
		var holds FleetCapacity
		for n, it := range taken {
			holds = holds.Plus(reservedShape(it.need))
			ids[n] = it.id
		}
		action := ps.buy(m, node.Offer, ActionBuy, holds, ids, nil)
		for _, it := range taken {
			ps.provisioning(it, nil, ptr(action))
		}
	}
}

// shortReason says why market m's purchases fall short of need: the pass's
// limits, or else the first exclusion whose removal lets an offer of the
// market cover it. An offer that covers it with every exclusion in place
// was held back by the quota room the pass's earlier purchases took.
func (ps *pass) shortReason(m ReserveMarket, need Requirement, reserve bool) CapacityWait {
	switch {
	case ps.hostRoom <= 0 || reserve && ps.reserveRoom <= 0:
		return WaitLimit
	case ps.s.BatchWait > 0:
		return WaitBatch
	case ps.room(m) <= 0:
		return WaitDeferred
	}
	offered := func(relax func(*OfferInputs)) bool {
		in := ps.s.Offers
		relax(&in)
		return slices.ContainsFunc(RankOffers(ps.p, need, reserve, in), func(o FleetOffer) bool {
			return o.Market == m.buyMarket() && o.Type.GPU == m.GPU
		})
	}
	switch {
	case offered(func(*OfferInputs) {}):
		return WaitQuota
	case offered(func(in *OfferInputs) { in.Cooldowns = nil }):
		return WaitCooldown
	case offered(func(in *OfferInputs) { in.Quotas = nil }):
		return WaitQuota
	case offered(func(in *OfferInputs) { in.OwnerPays = true }):
		return WaitMargin
	}
	return WaitNoOffer
}

// held is the capacity market v holds to serve: its serving, starting and
// requested hosts and this pass's purchases for it.
func (ps *pass) held(v *marketView) FleetCapacity {
	held := totalOf(ps.inMarket(v.m, FleetHost.held), func(h *FleetHost) FleetCapacity { return h.Usable })
	for _, n := range ps.planned {
		if n.market == v.m && !n.reserve {
			held = held.Plus(n.offer.Usable)
		}
	}
	return held
}

// anchored reports a host its market keeps whatever its target: one that
// runs work, that pending work takes, that an interruption protects, a
// rightsize's replacement until it serves, or the host it replaces until
// then.
func (ps *pass) anchored(h FleetHost) bool {
	return h.Containers > 0 || ps.claimed[h.ID] || h.Protected || h.Replaces != nil && h.State == FleetStarting ||
		slices.ContainsFunc(ps.hosts, func(r FleetHost) bool { return r.Replaces != nil && *r.Replaces == h.ID && r.State == FleetStarting })
}

// replaced reports a host whose rightsize replacement serves.
func (ps *pass) replaced(h FleetHost) bool {
	return h.Containers == 0 && slices.ContainsFunc(ps.hosts, func(r FleetHost) bool {
		return r.Replaces != nil && *r.Replaces == h.ID && serving(r)
	})
}

// keepOrder is market v's held hosts in the order its target keeps them:
// anchored hosts, then idle serving ones, those that never slept in the
// reserve first, then ones starting or updating their agent, so capacity
// on its way never retires capacity that serves, then ones not launched
// yet, which leave for free, then ones a serving rightsize replaced; each
// cheapest to serve first.
func (ps *pass) keepOrder(v *marketView) []*FleetHost {
	group := func(h *FleetHost) int {
		switch {
		case ps.anchored(*h):
			return 0
		case ps.replaced(*h):
			return 4
		case h.Requested:
			return 3
		case h.State == FleetStarting || h.UpdateDue:
			return 2
		}
		return 1
	}
	hosts := ps.inMarket(v.m, FleetHost.held)
	slices.SortStableFunc(hosts, func(a, b *FleetHost) int {
		return cmp.Or(cmp.Compare(group(a), group(b)), boolOrder(a.Slept, b.Slept), ps.retention(a, b))
	})
	return hosts
}

// idleHost is a kept idle serving host and the room its market keeps it
// for: its share of the running target and, on the one that keeps it, the
// floor.
type idleHost struct {
	host *FleetHost
	need FleetCapacity
}

// keep walks market v's held hosts in keep order and keeps each while the
// hosts kept before it, with this pass's purchases, do not cover the
// running target or none of them keeps the floor free; anchored hosts stay
// whatever the target. The order is the same on every replica and pass,
// so no two idle hosts can each count the other and both leave. What the
// kept hosts lack is resumed or bought, and the rest leave. Growth, here or
// by the work this pass bought or resumed for, raises the running target
// to what the market then holds for its window.
func (ps *pass) keep(v *marketView) []idleHost {
	if ps.grew(v.m) {
		ps.raise(v)
	}
	var kept FleetCapacity
	floorFree := v.Floor.Empty()
	for _, n := range ps.planned {
		if n.market == v.m && !n.reserve {
			// Only the work a purchase was bought for takes its room.
			free := n.offer.Usable
			if n.work {
				free = free.Minus(n.holds)
			}
			kept, floorFree = kept.Plus(n.offer.Usable), floorFree || free.Covers(v.Floor)
		}
	}
	var idle []idleHost
	var surplus []*FleetHost
	for _, h := range ps.keepOrder(v) {
		// A market without running headroom buys hosts for work alone, so
		// one not launched that nothing waits for any longer is not.
		if !ps.anchored(*h) && (kept.Covers(v.Running) && floorFree || h.Requested && v.Floor.Empty()) {
			surplus = append(surplus, h)
			continue
		}
		need := v.Running.Minus(kept).Clamp().Lower(h.Usable)
		if !floorFree && h.Usable.Minus(h.Load).Covers(v.Floor) {
			floorFree, need = true, need.Upper(v.Floor)
		}
		kept = kept.Plus(h.Usable)
		if serving(*h) && !ps.anchored(*h) && !h.UpdateDue {
			idle = append(idle, idleHost{host: h, need: need})
		}
	}
	if ps.grow(v, kept, floorFree) {
		ps.raise(v)
	}
	for _, h := range surplus {
		ps.leave(v, h)
	}
	return idle
}

// grew reports whether the pass bought or resumed a host to serve market m.
func (ps *pass) grew(m ReserveMarket) bool {
	return slices.ContainsFunc(ps.plan.Actions, func(a FleetAction) bool {
		return a.Market == m && (a.Kind == ActionBuy || a.Kind == ActionResume)
	})
}

// raise holds market v's running target at what it holds now, its window
// restarted.
func (ps *pass) raise(v *marketView) {
	v.Holds.Running = v.Holds.Running.raised(ps.held(v), ps.s.Now)
	v.Running = v.Holds.Running.Value
}

// grow resumes, then buys, what market v's kept hosts lack of its running
// target, and a host with its floor free when none of them has it, in a
// market that keeps running headroom, and reports whether it did. A resume
// serves sooner than any purchase and waits for no batch; a purchase waits
// while containers keep arriving.
func (ps *pass) grow(v *marketView, kept FleetCapacity, floorFree bool) bool {
	if v.Floor.Empty() {
		return false
	}
	need := CoverNeed{Aggregate: v.Running.Minus(kept).Clamp()}
	if !floorFree {
		need.Items, need.Aggregate = []CoverItem{{Shape: v.Floor, Count: 1}}, need.Aggregate.Minus(v.Floor).Clamp()
	}
	grew := false
	for ps.hostRoom > 0 && ps.room(v.m) > 0 && (!need.Aggregate.Empty() || len(need.Items) > 0) {
		h := ps.headroomReserve(v, need)
		if h == nil {
			break
		}
		ps.resume(v.m, h, nil)
		grew = true
		if len(need.Items) > 0 && h.Usable.Covers(v.Floor) {
			need.Items, need.Aggregate = nil, need.Aggregate.Plus(v.Floor)
		}
		need.Aggregate = need.Aggregate.Minus(h.Usable).Clamp()
	}
	short := need.Aggregate
	if len(need.Items) > 0 {
		short = short.Plus(v.Floor)
	}
	switch {
	case short.Empty():
		return grew
	case ps.hostRoom <= 0:
		v.shortfall, v.warmReason = short, WaitLimit
		return grew
	case ps.batching():
		v.shortfall, v.warmReason = short, WaitBatch
		return grew
	}
	r := ps.marketRanking(v.m, false)
	result := Cover(r.offers, need, servingCost(ps.p), ps.limits(min(ps.room(v.m), ps.hostRoom), r))
	for _, node := range result.Nodes {
		holds := node.Offer.Usable.Lower(need.Aggregate)
		if len(node.Placed) > 0 && node.Placed[0] > 0 {
			holds = holds.Upper(v.Floor)
		}
		ps.buy(v.m, node.Offer, ActionBuy, holds, nil, nil)
		grew = true
	}
	v.shortfall = need.Aggregate.Minus(result.Supplied).Clamp()
	if len(result.UnmetItems) > 0 {
		v.shortfall = v.shortfall.Plus(v.Floor)
	}
	if !v.shortfall.Empty() {
		v.warmReason = ps.shortReason(v.m, shapeNeed(v.m, v.shortfall.Lower(ps.p.LargestShape.Cap)), false)
	}
	return grew
}

// headroomReserve is the reserve of market v first in resume order that
// may resume for its running headroom: on the target agent build, its
// absence leaves a reserve that fits the largest shape, and serving costs
// no more than the cheapest purchase of need.
func (ps *pass) headroomReserve(v *marketView, need CoverNeed) *FleetHost {
	all := func(h *FleetHost) bool { return h.Usable.Covers(need.Aggregate) }
	var best *FleetHost
	for _, h := range ps.inMarket(v.m, func(h FleetHost) bool { return ps.ready(h, false) }) {
		if ps.leavesLarge(*h) && ps.resumePays(h, need) && (best == nil || ps.resumeOrder(h, best, all) < 0) {
			best = h
		}
	}
	return best
}

// resumeOrder orders the reserves a resume may take: one on the target
// agent build, then a stopped one before one still stopping, then one that
// holds all the resume is for, so one resume does, then the cheapest to
// serve.
func (ps *pass) resumeOrder(a, b *FleetHost, all func(*FleetHost) bool) int {
	return cmp.Or(boolOrder(!a.Current, !b.Current), boolOrder(a.State == FleetStopping, b.State == FleetStopping),
		boolOrder(!all(a), !all(b)), ps.retention(a, b))
}

// leave sends a host market v no longer keeps away: a requested one is not
// launched; a serving one returns to the reserve when it may (returns), or
// drains. A starting host, one updating its agent or one an interruption
// protects stays until a later pass.
func (ps *pass) leave(v *marketView, h *FleetHost) {
	switch {
	case h.Requested:
		h.State = FleetTerminating
		ps.act(FleetAction{Kind: ActionCancel, Market: v.m, Host: ptr(h.ID)})
		return
	case !serving(*h) || h.UpdateDue || h.Protected:
		return
	}
	if t, ok := ps.typeNamed(h.InstanceType); ok && ps.returns(v, *h) {
		mode := ReserveStop
		if h.HibernationConfigured && hibernates(t) {
			mode = ReserveHibernate
		}
		h.State, h.ReserveMode, h.Current = FleetPreparing, ptr(mode), false
		v.supply.counted = append(v.supply.counted, h)
		// It runs until it stops, so it keeps its host room.
		ps.reserveRoom--
		ps.act(FleetAction{Kind: ActionReturnToReserve, Market: v.m, Host: ptr(h.ID), Mode: ptr(mode)})
		return
	}
	h.State = FleetDraining
	ps.act(FleetAction{Kind: ActionDrain, Market: v.m, Host: ptr(h.ID)})
}

// returns reports whether leaving host h may stop into market v's reserve:
// EC2 can stop it, it lies within the size cap and outside a misplaced
// region, the reserve has room, and the market's reserve supply falls
// short of the stopped target, or holds none that fits the largest shape
// while h does.
func (ps *pass) returns(v *marketView, h FleetHost) bool {
	if !h.Stoppable || ps.reserveRoom <= 0 || !ps.p.LargestShape.Cap.Covers(h.Usable) || ps.misplaced(v, h.Region) {
		return false
	}
	total, large := v.supply.without(ps, v, h.ID)
	return !total.Covers(v.Stopped) || ps.fitsLargest(v, h) && !large
}

// reserveSupply is what holds a market's stopped target: the reserves that
// count, with the hosts that join them this pass, and the hosts resumed
// for headroom that no work has taken, which can stop back. A host serving
// work counts toward the running target alone.
type reserveSupply struct {
	counted, sleeping []*FleetHost
}

// supply counts market v's reserve supply once, before pending work takes
// room and hosts leave: the reserves that count, prepared on the target
// build, not interrupted and not misplaced, and its hosts resumed from the
// reserve that run no work.
func (ps *pass) supply(v *marketView) reserveSupply {
	return reserveSupply{
		counted: ps.inMarket(v.m, func(h FleetHost) bool { return !h.Protected && ps.placed(v)(h) }),
		sleeping: ps.inMarket(v.m, func(h FleetHost) bool {
			return h.held() && h.Slept && h.Containers == 0 && h.Stoppable && ps.p.LargestShape.Cap.Covers(h.Usable) && !ps.misplaced(v, h.Region)
		}),
	}
}

// without is what the supply holds without host skip, with the reserves
// this pass buys, and whether one of them fits market v's largest shape:
// counted reserves still reserves, and sleeping hosts still running or
// stopping back that no work holds.
func (r reserveSupply) without(ps *pass, v *marketView, skip HostID) (FleetCapacity, bool) {
	var total FleetCapacity
	large := v.Largest.Empty()
	add := func(usable FleetCapacity, fits bool) { total, large = total.Plus(usable), large || fits }
	for _, h := range r.counted {
		if h.ID != skip && h.reserve() && !v.retired[h.ID] {
			add(h.Usable, ps.fitsLargest(v, *h))
		}
	}
	for _, h := range r.sleeping {
		if h.ID != skip && h.held() && !ps.anchored(*h) {
			add(h.Usable, ps.fitsLargest(v, *h))
		}
	}
	for _, n := range ps.planned {
		if n.market == v.m && n.reserve {
			add(n.offer.Usable, v.fits(n.offer.Usable, n.offer.Type) && !ps.misplaced(v, n.offer.Region))
		}
	}
	return total, large
}

// reserves walks market v's counted reserves in reserve order and keeps
// each while those kept do not cover the stopped target or none fits the
// largest shape, and retires the rest: a pending one once the kept
// reserves cover both, a ready one once the kept ready ones do, and one
// that does not count once those and the sleeping hosts do. What the
// supply lacks is bought once no work waits in the market and arrivals
// have settled.
func (ps *pass) reserves(v *marketView) {
	ps.retireUnresumable(v)
	counted := slices.DeleteFunc(slices.Clone(v.supply.counted), func(h *FleetHost) bool { return !h.reserve() || v.retired[h.ID] })
	slices.SortStableFunc(counted, func(a, b *FleetHost) int { return ps.reserveOrder(v, a, b) })
	var kept, ready FleetCapacity
	large, readyLarge := v.Largest.Empty(), v.Largest.Empty()
	var surplus []*FleetHost
	for _, h := range counted {
		if kept.Covers(v.Stopped) && large {
			surplus = append(surplus, h)
			continue
		}
		kept, large = kept.Plus(h.Usable), large || ps.fitsLargest(v, *h)
		if h.resumable() {
			ready, readyLarge = ready.Plus(h.Usable), readyLarge || ps.fitsLargest(v, *h)
		}
	}
	readyCovers := ready.Covers(v.Stopped) && readyLarge
	for _, h := range surplus {
		if h.State != FleetStopping && (readyCovers || !h.resumable()) {
			ps.retire(v, h)
		}
	}
	// What does not count retires once the ready supply covers the target
	// without it; a counted reserve retires on the reserves alone, so a
	// host that may yet take work never retires one.
	for _, h := range v.supply.sleeping {
		if h.held() && !ps.anchored(*h) {
			ready, readyLarge = ready.Plus(h.Usable), readyLarge || ps.fitsLargest(v, *h)
		}
	}
	if ready.Covers(v.Stopped) && readyLarge {
		for _, h := range ps.inMarket(v.m, func(h FleetHost) bool { return h.reserve() && !h.Protected && h.State != FleetStopping }) {
			if !ps.placed(v)(*h) && !v.retired[h.ID] {
				ps.retire(v, h)
			}
		}
	}
	total, large := v.supply.without(ps, v, HostID{})
	short := v.Stopped.Minus(total).Clamp()
	var item FleetCapacity
	if !large {
		item = v.Largest
	}
	v.stoppedShort = short.Upper(item)
	switch {
	case v.stoppedShort.Empty():
		return
	case ps.waiting[v.m]:
		v.stoppedReason = WaitDemand
		return
	case ps.batching():
		v.stoppedReason = WaitBatch
		return
	}
	need := CoverNeed{Aggregate: short}
	if !item.Empty() {
		need = CoverNeed{Items: []CoverItem{{Shape: item, Disks: v.LargestDisks, Count: 1}}, Aggregate: short.Minus(item).Clamp()}
	}
	result := ps.buyReserves(v, need)
	v.stoppedShort = need.Aggregate.Minus(result.Supplied).Clamp()
	if len(result.UnmetItems) > 0 {
		v.stoppedShort = v.stoppedShort.Plus(item)
	}
	if !v.stoppedShort.Empty() {
		v.stoppedReason = ps.shortReason(v.m, shapeNeed(v.m, v.stoppedShort.Upper(item).Lower(ps.p.LargestShape.Cap)), true)
	}
}

// reserveOrder is the order market v keeps its reserves in: one that fits
// the largest shape first, ready before pending, cheapest to keep stopped.
func (ps *pass) reserveOrder(v *marketView, a, b *FleetHost) int {
	return cmp.Or(boolOrder(!ps.fitsLargest(v, *a), !ps.fitsLargest(v, *b)), boolOrder(!a.resumable(), !b.resumable()),
		cmp.Compare(ps.stoppedMicros(*a), ps.stoppedMicros(*b)), strings.Compare(a.ID.String(), b.ID.String()))
}

// retire terminates reserve h, or removes it before it launches.
func (ps *pass) retire(v *marketView, h *FleetHost) {
	v.retired[h.ID] = true
	ps.reserveRoom++
	ps.act(FleetAction{Kind: ActionRetireReserve, Market: v.m, Host: ptr(h.ID)})
}

// retireUnresumable retires the market's stopped reserves that stopped
// after their reclaim notice: they never resume, so they hold none of the
// target.
func (ps *pass) retireUnresumable(v *marketView) {
	for _, h := range ps.inMarket(v.m, func(h FleetHost) bool { return h.resumable() && h.Protected }) {
		ps.retire(v, h)
	}
}

// buyReserves buys the cheapest reserves that cover need in one cover: its
// item, the largest shape, on a host that hibernates where one fits it and
// its disks, and its aggregate on the cheapest room.
func (ps *pass) buyReserves(v *marketView, need CoverNeed) CoverResult {
	offers := ps.marketOffers(v.m, true)
	if len(need.Items) > 0 {
		offers = preferHibernating(offers, need.Items[0].Shape, need.Items[0].Disks)
	}
	result := Cover(offers, need, reserveCost(ps.p), ps.limits(min(ps.room(v.m), ps.reserveRoom, ps.hostRoom), ps.marketRanking(v.m, true)))
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

// preferHibernating drops the offers that fit shape and disks without
// hibernating while one that hibernates fits them, so the reserve that fits
// resumes from memory.
func preferHibernating(offers []FleetOffer, shape FleetCapacity, disks int) []FleetOffer {
	fits := func(o FleetOffer) bool { return o.Usable.Covers(shape) && o.Type.DiskSlots() >= disks }
	if !slices.ContainsFunc(offers, func(o FleetOffer) bool { return o.Hibernate && fits(o) }) {
		return offers
	}
	return slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool { return !o.Hibernate && fits(o) })
}

// rightsize replaces the kept idle host whose cheaper type pays back most:
// the hourly saving over the cost horizon must exceed what the new host
// costs while it provisions, and the new type must hold the room the
// market keeps the host for within the size rule. It runs only while
// market v holds steady and keeps no headroom beyond its floor, since
// headroom that follows the load leaves with it: its running target holds
// nothing above what it wants now, so it did not grow within its window,
// nothing in it starts or moves, and no container waits or arrives. The
// same type in another zone never replaces a host, so a Spot price move
// between zones buys nothing.
func (ps *pass) rightsize(v *marketView, idle []idleHost) {
	moving := slices.ContainsFunc(ps.plan.Actions, func(a FleetAction) bool {
		return a.Market == v.m && a.Kind != ActionRetireReserve
	})
	if ps.waiting[v.m] || ps.s.BatchWait > 0 || moving || !v.Want.Covers(v.Running) ||
		!v.Floor.Covers(v.Want.Minus(v.Load)) || len(ps.inMarket(v.m, starting)) > 0 ||
		ps.room(v.m) <= 0 || ps.hostRoom <= 0 {
		return
	}
	var best *FleetOffer
	var replaced idleHost
	var bestPayback int64
	for _, h := range idle {
		if o, payback := ps.replacement(v, h); o != nil && payback > bestPayback {
			best, replaced, bestPayback = o, h, payback
		}
	}
	if best != nil {
		ps.buy(v.m, *best, ActionRightsize, replaced.need, nil, ptr(replaced.host.ID))
	}
}

// replacement is the offer of another type that pays back most for
// replacing idle host h, with its payback, or nil: it holds the room the
// market keeps h for within the size rule, and what it saves over the cost horizon, priced as
// purchases are, exceeds what it costs while it provisions. A host whose
// replacement EC2 refused within the cost horizon has none.
func (ps *pass) replacement(v *marketView, h idleHost) (*FleetOffer, int64) {
	cost, known := ps.hostCost(*h.host)
	refused := h.host.RightsizeRefusedAt != nil && ps.s.Now.Sub(*h.host.RightsizeRefusedAt) < ps.p.CostHorizon
	if h.host.Protected || refused || !known || h.need.Empty() {
		return nil, 0
	}
	var best *FleetOffer
	var bestPayback int64
	provision, serving := int64(ps.p.Provision/time.Second), servingCost(ps.p)
	r := ps.marketRanking(v.m, false)
	for _, o := range r.offers {
		payback := cost - serving(o) - o.HourlyMicros*provision
		if o.Type.Name != h.host.InstanceType && o.Usable.Covers(h.need) && ps.p.sized(o, h.need, 0, r.refs, r.soon) && payback > bestPayback {
			best, bestPayback = &o, payback
		}
	}
	return best, bestPayback
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

// report is the market's published plan. A market whose work waits on the
// fleet limit or for arrivals to settle says so; otherwise a running
// shortfall names its reason, then a reserve shortfall its own.
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
		Market: v.m, Load: v.Load, WarmTarget: v.Running.Minus(v.Load).Clamp(),
		WarmFree: ps.warmFree(v), WarmPending: ps.warmPending(v),
		StoppedTarget: v.Stopped, ReserveReady: ready, ReservePending: reserve.Minus(ready).Clamp(),
		Shortfall: v.shortfall, StoppedShortfall: v.stoppedShort, Holds: v.Holds, States: ps.states(v.m),
	}
	switch {
	case ps.limited[v.m]:
		plan.Reason = WaitLimit
	case ps.batched[v.m]:
		plan.Reason = WaitBatch
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

package compute

import (
	"cmp"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/AmbientWare/lazycloud/internal/billing"

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
	// Load is what its live containers reserve; Packed, on a GPU host, what
	// CPU work reserves, which counts as the load of the CPU Spot market
	// and which GPU work evicts.
	Load, Packed FleetCapacity
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
	// PhaseAt is when the host entered its phase; a serving host's is when
	// it became ready.
	PhaseAt time.Time
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
	return h.room()
}

// room is what the host's live containers leave of it.
func (h FleetHost) room() FleetCapacity { return h.Usable.Minus(h.Load).Clamp() }

// resumable is a stopped reserve.
func (h FleetHost) resumable() bool {
	return h.State == FleetStopped || h.State == FleetHibernateUnverified || h.State == FleetImageSaved
}

// reserve is a host held, or being made, a reserve.
func (h FleetHost) reserve() bool {
	return h.resumable() || h.State == FleetPreparing || h.State == FleetStopping
}

// capacity is the host's room, its disk slots those its type's data volume
// holds. Placement gives a host due an agent update none of it (forWork);
// the headroom it holds keeps it while the update runs.
func (h FleetHost) capacity() HostCapacity {
	free := h.room()
	room := HostCapacity{
		Host: h.ID, Kind: KindPlatform, Provider: ProviderAWS, Region: h.Region, Zone: h.Zone, ZoneID: h.ZoneID,
		Market: h.Market, GPUType: h.GPU, GPUCount: h.Usable.GPUs,
		CPUMillis: h.Usable.CPUMillis, MemoryBytes: h.Usable.MemoryBytes,
		FreeCPUMillis: free.CPUMillis, FreeMemoryBytes: free.MemoryBytes, FreeGPUs: free.GPUs, HandsBack: h.HandsBack,
		Packed: h.Packed,
	}
	if t, ok := CatalogTypeNamed(h.InstanceType); ok {
		room.FreeDiskSlots = max(0, t.DiskSlots()-h.UsedDisks)
	}
	return room
}

// forWork is room as placement gives it to pending work on host h: none
// while h is due an agent update.
func forWork(h FleetHost, room HostCapacity) HostCapacity {
	if h.UpdateDue {
		room.FreeCPUMillis, room.FreeMemoryBytes, room.FreeGPUs, room.FreeDiskSlots = 0, 0, 0, 0
	}
	return room
}

// DemandGroup is pending platform containers with one requirement.
type DemandGroup struct {
	Need       Requirement
	Containers []PendingContainer
}

// PendingContainer is one pending container and the host bought for it.
type PendingContainer struct {
	ID uuid.UUID
	// Since is when the container was created.
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
	// disks one of them attached; Started the largest within the WarmWindow,
	// which sizes the warm floor, and Burst the largest batch of arrivals
	// within it, which a market with a floor keeps room for; Arrived is
	// what the CPU Spot market's placed containers created within the
	// ArrivalWindow reserve, less their largest batch.
	Recent, Started, Burst, Arrived map[ReserveMarket]FleetCapacity
	RecentDisks                     map[ReserveMarket]int
	Offers                          OfferInputs
	// HostRoom is how many more hosts may run or start, reserves being
	// prepared among them; ReserveRoom how many more may be held stopped.
	HostRoom    int
	ReserveRoom int
	// BatchWait is how long the arrival batch stays open; zero once it has
	// closed.
	BatchWait time.Duration
	// Holds are each market's held targets, from the published plans.
	Holds map[ReserveMarket]MarketHolds
	// AtLossSpent is what the platform's hosts bought at a loss cost to run
	// today, in micros.
	AtLossSpent int64
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
	// ActionCancel removes a requested host that no pending container or
	// headroom waits for any longer.
	ActionCancel FleetActionKind = "cancel"
	// ActionRightsize buys Offer to replace an idle Host; the host stays
	// until the replacement serves.
	ActionRightsize FleetActionKind = "rightsize"
	// ActionHandBack has an on-demand Host hand Spot-tolerant work back to
	// Spot; ActionLend has it lend that work room again.
	ActionHandBack FleetActionKind = "hand_back"
	ActionLend     FleetActionKind = "lend"
)

// ActionReason says why a pass took an action.
type ActionReason string

const (
	// ReasonWork is a resume or purchase for pending containers.
	ReasonWork ActionReason = "work"
	// ReasonFloor and ReasonLoad are a resume or purchase for headroom
	// alone: parts of it that fit no kept host, or the rest in aggregate.
	ReasonFloor ActionReason = "floor"
	ReasonLoad  ActionReason = "load"
	// ReasonSpotWait is an on-demand purchase for Spot-tolerant GPU work
	// that waited the Spot wait while no Spot offer could host it.
	ReasonSpotWait ActionReason = "spot_wait"
	// ReasonReserve is a reserve purchase, or a return to the reserve, for
	// the stopped target; ReasonLargest for a reserve that fits the largest
	// shape.
	ReasonReserve ActionReason = "reserve"
	ReasonLargest ActionReason = "largest"
	// ReasonCheaper is a rightsize to a type that pays back.
	ReasonCheaper ActionReason = "cheaper"
	// ReasonIdle is a drain of a host idle past its timeout.
	ReasonIdle ActionReason = "idle"
	// ReasonInterrupted retires a reserve stopped after its reclaim notice,
	// ReasonSurplus one beyond the stopped target.
	ReasonInterrupted ActionReason = "interrupted"
	ReasonSurplus     ActionReason = "surplus"
	// ReasonUnclaimed cancels a requested host nothing waits for.
	ReasonUnclaimed ActionReason = "unclaimed"
	// ReasonSpotHosts hands Spot-tolerant work back once Spot can host it;
	// ReasonSpotRefuses lends again once it cannot.
	ReasonSpotHosts   ActionReason = "spot_hosts"
	ReasonSpotRefuses ActionReason = "spot_refuses"
)

// Message is the reason as the pass logs it.
func (r ActionReason) Message() string {
	switch r {
	case ReasonWork:
		return "pending work"
	case ReasonFloor:
		return "the warm floor"
	case ReasonLoad:
		return "headroom for load and arrivals"
	case ReasonSpotWait:
		return "Spot refused past the Spot wait"
	case ReasonReserve:
		return "the stopped target"
	case ReasonLargest:
		return "a reserve that fits the largest shape"
	case ReasonCheaper:
		return "a cheaper type pays back"
	case ReasonIdle:
		return "idle past its timeout"
	case ReasonInterrupted:
		return "stopped after its reclaim notice"
	case ReasonSurplus:
		return "beyond the stopped target"
	case ReasonUnclaimed:
		return "nothing waits for it"
	case ReasonSpotHosts:
		return "Spot can host its work"
	case ReasonSpotRefuses:
	}
	return "Spot cannot host its work"
}

// FleetAction is one decision, naming a host or an offer, and why.
type FleetAction struct {
	Kind   FleetActionKind
	Reason ActionReason
	Market ReserveMarket
	Host   *HostID
	Offer  *FleetOffer
	Mode   *ReserveMode
	// Containers are the pending containers a resume or purchase is for.
	Containers []uuid.UUID
	// Holds is what a purchase must hold in whichever pool it launches:
	// the containers or headroom it was bought for, or the reserve room.
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

// MarketPlan is one market's targets and measures.
type MarketPlan struct {
	Market ReserveMarket
	// Load is what the market's containers hold now: running and pending.
	Load FleetCapacity
	// WarmTarget is the headroom the running target holds beyond the load;
	// WarmFree the room the market's serving hosts have free, and
	// WarmPending what its starting hosts and this pass's purchases bring.
	WarmTarget, WarmFree, WarmPending FleetCapacity
	StoppedTarget                     FleetCapacity
	ReserveReady, ReservePending      FleetCapacity
	// Shortfall is what the running target lacks that nothing bought;
	// StoppedShortfall what the reserves lack.
	Shortfall, StoppedShortfall FleetCapacity
	// Holds are the market's held targets, which the next pass reads back.
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

// PlanFleet decides one pass over a snapshot, in stages. On-demand hosts
// decide whether they lend to Spot-tolerant work. Each market computes its
// targets. The cover places pending work: onto ready room, then starting
// hosts, then reserves resumed for it, then purchases, which wait while
// containers keep arriving. Each market then keeps its hosts in one order
// until they cover the running target and the rest leave, grows what the
// kept ones lack, keeps its reserves to the stopped target and buys or
// retires the difference, and replaces a kept idle host a cheaper type
// pays back on.
func PlanFleet(p Policy, s FleetSnapshot) FleetPlan {
	ps := &pass{
		p: p, s: s, hosts: slices.Clone(s.Hosts),
		used: map[ReserveMarket]int{}, waiting: map[ReserveMarket]bool{}, limited: map[ReserveMarket]bool{}, batched: map[ReserveMarket]bool{},
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
	views := ps.targets(items)
	ps.cover(items)
	for _, v := range views {
		ps.keep(v)
		ps.grow(v)
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
	offers                    map[string]ranking
	// classReads is the largest read pending work of each demand class
	// makes, by class key.
	classReads map[string]int64
	// spotOffers holds spotOffered's answers.
	spotOffers map[spotNeed]bool
	byMarket   map[ReserveMarket]*marketView
	// claimed are hosts pending work fits, serving or starting.
	claimed map[HostID]bool
	// partsShort are each market's parts of headroom that fit no serving
	// host once pending work has taken its room.
	partsShort map[ReserveMarket][]FleetCapacity
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
	// holds is the work the host is bought for; the rest of it is room.
	holds FleetCapacity
}

// marketView is one market during the pass: its target, and what the pass
// decided for it.
type marketView struct {
	marketTarget
	// kept are the hosts the keep order kept; capacity is what they and
	// this pass's purchases hold, covered what the kept serving hosts hold
	// less the free room of hosts that are leaving, and unplaced the parts
	// of this pass's headroom that fit free on none of the kept hosts and
	// purchases.
	kept              []*FleetHost
	capacity, covered FleetCapacity
	unplaced          []FleetCapacity
	retired           map[HostID]bool
	// shortfall and stoppedShort are what the pass could not cover, and
	// runningReason and stoppedReason why.
	shortfall, stoppedShort      FleetCapacity
	runningReason, stoppedReason CapacityWait
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

// busy reports a host its work holds up: one running containers, but a GPU
// host only while one of them uses a GPU. CPU work packed into a GPU host
// never keeps it up; the host leaves on its market's keep window and the
// work is queued again.
func busy(h FleetHost) bool {
	if h.GPU != "" {
		return h.Load.GPUs > 0
	}
	return h.Containers > 0
}

// coverItem is one pending container during the cover: its need, the
// market its demand belongs to, the host bought for it and its wait.
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

// marketNeed is what work of market m asks of an offer.
func marketNeed(m ReserveMarket) Requirement {
	need := Requirement{Preemptible: m.Preemptible}
	if m.GPU != "" {
		need.GPUs = []string{m.GPU}
	}
	return need
}

// shapeNeed is a shape of market m as a requirement.
func shapeNeed(m ReserveMarket, shape FleetCapacity) Requirement {
	return needOf(marketNeed(m), shape)
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
// region saves. A reserve there is the first to retire once the market
// holds more than its stopped target; it is never bought again for its
// region, since EC2 may have refused the read region and the launch walk
// placed it where capacity was.
func (ps *pass) misplaced(v *marketView, region string) bool {
	r := ps.s.Offers.marketRead(v.m)
	if r == nil || region == r.Region {
		return false
	}
	offers := preferHibernating(ps.marketOffers(v.m, true), v.largest, v.largestDisks)
	i := slices.IndexFunc(offers, func(o FleetOffer) bool { return v.fits(o.Usable, o.Type) })
	saved := readMicros(ps.p, r, region) * int64(ps.p.CostHorizon/time.Second)
	return i >= 0 && offers[i].Region == r.Region && offers[i].HourlyMicros*int64(ps.p.Provision/time.Second) < saved
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
func (ps *pass) buy(m ReserveMarket, o FleetOffer, kind FleetActionKind, reason ActionReason, holds FleetCapacity, containers []uuid.UUID,
	replaces *HostID) int {
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
	ps.planned = append(ps.planned, plannedHost{market: reserveMarketOf(o.Market, o.Type.GPU), offer: o, reserve: reserve, holds: holds})
	ps.start(o.Region, o.Type.Name, o.Market)
	return ps.act(FleetAction{Kind: kind, Reason: reason, Market: m, Host: replaces, Offer: &o, Mode: mode, Containers: containers, Holds: holds})
}

func (ps *pass) resume(m ReserveMarket, h *FleetHost, reason ActionReason, containers []uuid.UUID) {
	// A stopping reserve holds its host room and quota already.
	if h.State != FleetStopping {
		ps.hostRoom--
		ps.start(h.Region, h.InstanceType, h.Market)
	}
	h.State = FleetStarting
	ps.reserveRoom++
	ps.act(FleetAction{Kind: ActionResume, Reason: reason, Market: m, Host: ptr(h.ID), Containers: containers})
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

// take reserves it on the bin ChooseHost picks, as placement would, and
// returns its index, or -1. rooms mirror the bins' room for work.
func take(bins []bin, rooms []HostCapacity, it coverItem, floor FleetCapacity) int {
	i := ChooseHost(rooms, it.need, floor)
	if i >= 0 {
		bins[i].room.Reserve(it.need)
		rooms[i] = forWork(*bins[i].host, bins[i].room)
	}
	return i
}

// binRooms are the bins' room for pending work.
func binRooms(bins []bin) []HostCapacity {
	rooms := make([]HostCapacity, len(bins))
	for i, b := range bins {
		rooms[i] = forWork(*b.host, b.room)
	}
	return rooms
}

// cover places pending containers. Each takes room on the host bought for
// it while that serves with room, else on the ready host placement would
// give it, else on the host bought for it while that starts, else on a
// starting host that serves sooner than a ready reserve of its market
// would. Ready reserves resume for what is left, and purchases cover the
// rest.
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
	// readyRooms and comingRooms are the bins' room, which pending
	// containers take as placement would: by ChooseHost.
	readyRooms, comingRooms := binRooms(ready), binRooms(coming)
	floor := ps.p.OnDemand.Warm.Floor
	var left []coverItem
	for _, it := range items {
		if it.need.Machine != "" || it.need.Connection != nil {
			continue
		}
		if i := ps.boughtBin(it, ready, readyRooms); i >= 0 {
			// It lands on the host bought for it, as placement puts it there
			// first; the wait keeps naming the host until placement does.
			ready[i].room.Reserve(it.need)
			readyRooms[i] = forWork(*ready[i].host, ready[i].room)
			ready[i].host.Load = ready[i].host.Load.Plus(reservedShape(it.need))
			ps.claimed[ready[i].host.ID] = true
			ps.plan.Waits[it.wait].Host = it.bought
			continue
		}
		if i := take(ready, readyRooms, it, floor); i >= 0 {
			// The room it takes is load, not headroom.
			ready[i].host.Load = ready[i].host.Load.Plus(reservedShape(it.need))
			ps.claimed[ready[i].host.ID] = true
			continue
		}
		if it.bought != nil {
			if i := slices.IndexFunc(coming, func(b bin) bool { return b.host.ID == *it.bought }); i >= 0 {
				coming[i].room.Reserve(it.need)
				comingRooms[i] = forWork(*coming[i].host, coming[i].room)
				coming[i].host.Load = coming[i].host.Load.Plus(reservedShape(it.need))
				ps.claimed[coming[i].host.ID] = true
				ps.provisioning(it, ptr(coming[i].host.ID), nil)
				continue
			}
			// A stale reserve resumed for it serves once its agent updates.
			if i := slices.IndexFunc(ready, func(b bin) bool { return b.host.ID == *it.bought && b.host.UpdateDue }); i >= 0 {
				ps.provisioning(it, ptr(ready[i].host.ID), nil)
				continue
			}
		}
		// CPU work packs only into a GPU host that serves, so it never
		// waits on one booting.
		booting := it
		booting.need.Packs = false
		if i := ps.comingFor(coming, comingRooms, booting, floor); i >= 0 {
			coming[i].host.Load = coming[i].host.Load.Plus(reservedShape(it.need))
			ps.claimed[coming[i].host.ID] = true
			ps.provisioning(it, ptr(coming[i].host.ID), nil)
			continue
		}
		left = append(left, it)
	}
	ps.partsShort = map[ReserveMarket][]FleetCapacity{}
	for m, v := range ps.byMarket {
		var rooms []FleetCapacity
		for i, b := range ready {
			if b.host.market() == m {
				rooms = append(rooms, roomOf(readyRooms[i]))
			}
		}
		ps.partsShort[m] = unplaced(rooms, v.wantParts)
	}
	ps.buyFor(ps.resumeFor(left))
}

// roomOf is the capacity a host's room for work has free.
func roomOf(r HostCapacity) FleetCapacity {
	return FleetCapacity{CPUMillis: r.FreeCPUMillis, MemoryBytes: r.FreeMemoryBytes, GPUs: r.FreeGPUs}
}

// comingFor is the starting host pending container it waits for, with its
// room taken, or -1: none has room, or the one placement would pick is
// expected to serve later than a ready reserve of the container's market
// that fits it, which resumeFor takes instead. A host that has booted for
// a while serves soon; one just requested does not.
func (ps *pass) comingFor(coming []bin, rooms []HostCapacity, it coverItem, floor FleetCapacity) int {
	i := ChooseHost(rooms, it.need, floor)
	if i < 0 {
		return -1
	}
	h := coming[i].host
	if ps.remaining(*h) > ps.resumeTime(it.need) && ps.reserveFits(it) {
		return -1
	}
	coming[i].room.Reserve(it.need)
	rooms[i] = forWork(*h, coming[i].room)
	return i
}

// remaining is how long starting host h is expected to take to serve: its
// market's launch time less the time since it began, and nothing once that
// has passed.
func (ps *pass) remaining(h FleetHost) time.Duration {
	launch := ps.p.Starts.Launch
	if h.GPU != "" {
		launch = ps.p.Starts.GPULaunch
	}
	return max(0, launch-ps.s.Now.Sub(h.PhaseAt))
}

// resumeTime is how long a resumed reserve takes to serve need.
func (ps *pass) resumeTime(need Requirement) time.Duration {
	if need.GPUsNeeded() > 0 {
		return ps.p.Starts.GPUResume
	}
	return ps.p.Starts.Resume
}

// reserveFits reports a ready reserve of pending container it's market with
// room for it.
func (ps *pass) reserveFits(it coverItem) bool {
	shape := reservedShape(it.need)
	return slices.ContainsFunc(ps.hosts, func(h FleetHost) bool {
		return h.market() == it.market && ps.ready(h, true) && h.Usable.Covers(shape) && ps.room(it.market) > 0
	})
}

// boughtBin is the ready bin of the serving host bought for a pending
// container while it has room for it, or -1.
func (ps *pass) boughtBin(it coverItem, ready []bin, rooms []HostCapacity) int {
	if it.bought == nil {
		return -1
	}
	i := slices.IndexFunc(ready, func(b bin) bool { return b.host.ID == *it.bought })
	if i < 0 || !rooms[i].Fits(it.need) {
		return -1
	}
	return i
}

// provisioning records that a pending container waits for a host.
func (ps *pass) provisioning(it coverItem, host *HostID, action *int) {
	w := &ps.plan.Waits[it.wait]
	w.Wait, w.Host, w.Action = ptr(WaitProvisioning), host, action
}

// waits records why a pending container no host serves or is coming for
// waits; its market waits with it.
func (ps *pass) waits(it coverItem, wait CapacityWait) {
	ps.plan.Waits[it.wait].Wait = ptr(wait)
	ps.waiting[it.market] = true
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
		kind, reason := ActionLend, ReasonSpotRefuses
		if back {
			kind, reason = ActionHandBack, ReasonSpotHosts
		}
		ps.act(FleetAction{Kind: kind, Reason: reason, Market: h.market(), Host: ptr(h.ID)})
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

// resumeFor resumes ready reserves for the pending containers nothing
// running holds, until none takes any, and returns what is left. A resume
// waits for no batch: it is faster than any purchase.
func (ps *pass) resumeFor(items []coverItem) []coverItem {
	for len(items) > 0 && ps.hostRoom > 0 {
		h, taken := ps.bestReserve(items)
		if h == nil {
			break
		}
		var ids []uuid.UUID
		for _, n := range taken {
			ids = append(ids, items[n].id)
		}
		ps.resume(items[taken[0]].market, h, ReasonWork, ids)
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

// bestReserve is the ready reserve to resume for the pending containers,
// and the ones, by index, it takes first fit: one of the first container's
// market first, then a stopped one,
// then the one that takes the most containers, then, while its market's
// floor shape fits no serving host, one with room for that too, so one
// resume serves the burst and the start after it, then the one whose
// region they read least across, then one that takes every container, so
// one resume does; then the smallest and the cheapest. Spot work takes an
// on-demand reserve only while no Spot offer can host it, and a CPU one
// only while it is lendable.
func (ps *pass) bestReserve(items []coverItem) (*FleetHost, []int) {
	var best *FleetHost
	var bestTaken []int
	var bestRead int64
	var bestFloor bool
	spotWork := slices.ContainsFunc(items, func(it coverItem) bool { return it.need.Preemptible })
	for i := range ps.hosts {
		h := &ps.hosts[i]
		if !ps.ready(*h, true) {
			continue
		}
		onDemand := h.Market == MarketOnDemand
		lendsCPU := spotWork && onDemand && h.GPU == "" && ps.lendable(*h)
		lends := func(need Requirement) bool { return !onDemand || (lendsCPU || h.GPU != "") && !ps.spotOffered(need) }
		b := bin{host: h, room: h.capacity()}
		var taken []int
		for n, it := range items {
			if ps.room(it.market) <= 0 || !b.room.Fits(it.need) || it.need.Preemptible && !lends(it.need) {
				continue
			}
			b.room.Reserve(it.need)
			taken = append(taken, n)
		}
		if len(taken) == 0 {
			continue
		}
		var read int64
		for _, n := range taken {
			read += readMicros(ps.p, items[n].need.Read, h.Region)
		}
		short := ps.partsShort[h.market()]
		floor := len(short) > 0 && placesAll([]FleetCapacity{roomOf(b.room)}, short)
		if best == nil || cmp.Or(boolOrder(h.market() != items[taken[0]].market, best.market() != items[bestTaken[0]].market),
			boolOrder(h.State == FleetStopping, best.State == FleetStopping),
			cmp.Compare(len(bestTaken), len(taken)), boolOrder(!floor, !bestFloor), cmp.Compare(read, bestRead),
			boolOrder(len(taken) < len(items), len(bestTaken) < len(items)), shapeOrder(h.Usable, best.Usable), ps.retention(h, best)) < 0 {
			best, bestTaken, bestRead, bestFloor = h, taken, read, floor
		}
	}
	return best, bestTaken
}

// ready reports whether reserve h can resume now: one whose start EC2
// refused waits out the capacity cooldown; a stopped one resumes for
// anything, whatever agent build it stopped on, since its agent updates
// itself before it serves; for work, a stopping one resumes when it serves
// sooner than a purchase. A stopping reserve's quota is counted already.
func (ps *pass) ready(h FleetHost, work bool) bool {
	stopping := h.State == FleetStopping && h.StoppedAt != nil && !ps.stalled(h)
	if h.Protected || h.StartRefused {
		return false
	}
	if h.resumable() {
		return ps.startable(h)
	}
	if !stopping {
		return false
	}
	st := ps.p.Starts
	launch, serves := st.Launch, st.Resume
	if h.GPU != "" {
		launch, serves = st.GPULaunch, st.GPUResume
	}
	serves += max(0, h.StoppedAt.Add(st.SpotDisable).Sub(ps.s.Now))
	return work && serves < launch
}

// stalled reports a resumed Spot reserve whose start still waits for EC2
// to disable its request past SpotDisableMax: work stops waiting for it.
func (ps *pass) stalled(h FleetHost) bool {
	return h.StoppedAt != nil && ps.s.Now.Sub(*h.StoppedAt) > ps.p.Starts.SpotDisableMax
}

// leavesLarge reports whether the reserves of h's market without h still
// hold one that fits its largest shape, where h fits it.
func (ps *pass) leavesLarge(h FleetHost) bool {
	v := ps.byMarket[h.market()]
	return !ps.fitsLargest(v, h) || ps.holdsLargest(v, h.ID)
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
	return usable.Covers(v.largest) && t.DiskSlots() >= v.largestDisks
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

// resumePays reports whether resuming reserve h for need costs no more
// over the cost horizon than the cheapest purchase that covers it. The
// resume is priced without the root it pays stopped as well, the purchase
// with what it runs while it provisions. With no purchase that covers it, a
// resume is the only way.
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
// the ready on-demand reserves left without it still cover their stopped
// target and hold one that fits the largest shape.
func (ps *pass) lendable(h FleetHost) bool {
	v := ps.byMarket[h.market()]
	ready := totalOf(ps.inMarket(v.m, func(r FleetHost) bool { return r.ID != h.ID && r.resumable() && ps.counted(v)(r) }),
		func(r *FleetHost) FleetCapacity { return r.Usable })
	return ready.Covers(v.holds.Stopped.Load) && ps.leavesLarge(h)
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
func coverNeed(c *demandClass, only func(itemGroup) bool) (CoverNeed, []int) {
	var need CoverNeed
	var at []int
	for i, g := range c.groups {
		if len(g.items) == 0 || only != nil && !only(g) {
			continue
		}
		need.Items = append(need.Items, CoverItem{Shape: g.shape, Disks: g.disks, Count: len(g.items)})
		at = append(at, i)
	}
	return need, at
}

// buyFor covers the pending containers no running host or resumed reserve
// holds with new hosts, per class of containers that share offers, unless
// containers are still arriving or the fleet limit is reached.
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

// buyClass covers what is left of class c from offers. The CPU Spot market
// buys Spot hosts that each hold at least its headroom, up to the largest
// shape's cap: Spot costs less per core on larger hosts, so a growing
// market buys hosts that grow with it, and the room they leave is
// headroom. What those cannot take, under a quota or a cooldown, any offer
// covers. On-demand
// prices scale with size, so on-demand hosts hold what the cover needs.
// GPU work buys its models in its preference order: Spot-tolerant work on
// Spot, on-demand only what no Spot offer of any model can host, and, once
// it has waited the Spot wait, a host of its own whatever the margin
// (buyAtLoss).
func (ps *pass) buyClass(c *demandClass, r ranking) {
	m, offers := c.market, r.offers
	if m.GPU != "" {
		models := ps.gpuModels(c.need, offers)
		buy := func(market Market, only func(itemGroup) bool) {
			for _, model := range models {
				ps.coverClass(c, ReserveMarket{Preemptible: m.Preemptible, GPU: model}, slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool {
					return o.Type.GPU != model || o.Market != market
				}), r, only, false)
			}
		}
		if !m.Preemptible {
			buy(MarketOnDemand, nil)
			return
		}
		buy(MarketSpot, nil)
		buy(MarketOnDemand, func(g itemGroup) bool { return !ps.spotOffered(g.items[0].need) })
		ps.buyAtLoss(c)
		return
	}
	if v := ps.byMarket[m]; v != nil && m.spotCPU() {
		least := v.headroom.Lower(ps.p.LargestShape.Cap)
		ps.coverClass(c, m, slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool {
			return o.Market != MarketSpot || !o.Usable.Covers(least)
		}), r, nil, false)
	}
	ps.coverClass(c, m, offers, r, nil, false)
}

// buyAtLoss buys each container of Spot-tolerant GPU class c that waited
// the Spot wait while no Spot offer of a model it accepts can host it
// within the margin a host of its own with the margin set aside: Spot of
// any model it accepts first, then on-demand.
// The platform buys at a loss only while today's at-loss hosts' cost keeps
// the most it can lose, LossCapPercent of that cost, under LossBudget, and
// only hosts whose cost the container alone, at its rate, bills at least
// 100-LossCapPercent percent of; work it does not buy for waits for Spot.
func (ps *pass) buyAtLoss(c *demandClass) {
	waited := func(it coverItem) bool { return ps.s.Now.Sub(it.since) >= ps.p.SpotWait && !ps.spotOffered(it.need) }
	if !slices.ContainsFunc(c.groups, func(g itemGroup) bool { return slices.ContainsFunc(g.items, waited) }) {
		return
	}
	in := ps.s.Offers
	if !in.OwnerPays && ps.s.AtLossSpent*ps.p.LossCapPercent/100 >= ps.p.LossBudget {
		return
	}
	in.WaiveMargin = true
	r := ranking{offers: RankOffers(ps.p, c.need, false, in), refs: sizeRefs(ps.p, c.need, false, in)}
	if !in.OwnerPays {
		rates := indexRates(in.Rates)
		need := c.need
		r.offers = slices.DeleteFunc(r.offers, func(o FleetOffer) bool {
			rate, ok := rates[billing.RateClassFor(need.Region != "" || need.Zone != "", true)][billing.GPUType(o.Type.GPU)]
			worked := FleetCapacity{CPUMillis: need.CPUMillis, MemoryBytes: need.MemoryBytes, GPUs: need.GPUsNeeded()}
			return !ok || hourRevenue(rate, worked)*100 < o.HourlyMicros*(100-ps.p.LossCapPercent)*1000
		})
	}
	r.soon = RankOffers(ps.p, c.need, false, availableSoon(ps.p, in))
	for i := range c.groups {
		g := &c.groups[i]
		for _, it := range slices.Clone(g.items) {
			if !waited(it) {
				continue
			}
			alone := &demandClass{need: c.need, key: c.key, market: c.market, groups: []itemGroup{{shape: g.shape, disks: g.disks, items: []coverItem{it}}}}
			for _, market := range []Market{MarketSpot, MarketOnDemand} {
				for _, model := range ps.gpuModels(c.need, r.offers) {
					ps.coverClass(alone, ReserveMarket{Preemptible: true, GPU: model}, slices.DeleteFunc(slices.Clone(r.offers), func(o FleetOffer) bool {
						return o.Type.GPU != model || o.Market != market
					}), r, nil, true)
				}
			}
			if len(alone.groups[0].items) == 0 {
				g.items = slices.DeleteFunc(g.items, func(o coverItem) bool { return o.id == it.id })
			}
		}
	}
}

// coverClass buys from offers the cheapest cover of what is left of class
// c's groups, of those only keeps when it is set, each host sized against
// r for what it holds; atLoss marks purchases past the Spot wait.
func (ps *pass) coverClass(c *demandClass, m ReserveMarket, offers []FleetOffer, r ranking, only func(itemGroup) bool, atLoss bool) {
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
		reason := ReasonWork
		if atLoss {
			reason = ReasonSpotWait
		}
		action := ps.buy(m, node.Offer, ActionBuy, reason, holds, ids, nil)
		for _, it := range taken {
			ps.provisioning(it, nil, ptr(action))
		}
	}
}

// shortReason says why market m's purchases fall short of need: the pass's
// limits, or else the first exclusion whose removal lets an offer of
// the market cover it. An offer that covers it with every exclusion in
// place was held back by the quota room the pass's earlier purchases took.
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
		Market: v.m, Load: v.load, WarmTarget: v.headroom, WarmFree: ps.warmFree(v), WarmPending: ps.warmPending(v),
		StoppedTarget: v.holds.Stopped.Load, ReserveReady: ready, ReservePending: reserve.Minus(ready).Clamp(),
		Shortfall: v.shortfall, StoppedShortfall: v.stoppedShort, Holds: v.holds, States: ps.states(v.m),
	}
	switch {
	case ps.limited[v.m]:
		plan.Reason = WaitLimit
	case ps.batched[v.m]:
		plan.Reason = WaitBatch
	case !plan.Shortfall.Empty():
		plan.Reason = v.runningReason
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

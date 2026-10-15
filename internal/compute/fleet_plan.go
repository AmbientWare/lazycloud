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
	// IdleSince is when the serving host became idle, as last recorded;
	// BusySince when the newest container Containers counts was placed.
	IdleSince, BusySince *time.Time
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
// its warm slots keep it while the update runs.
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
	// disks one of them attached; Builds is the largest its finished build
	// containers reserved within its BuildWindow; Arrived is what the CPU
	// Spot market's placed containers created within the ArrivalWindow
	// reserve, less their largest batch.
	Recent, Builds, Arrived map[ReserveMarket]FleetCapacity
	RecentDisks             map[ReserveMarket]int
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
	// ActionCancel removes a requested host that no pending container or
	// warm slot waits for any longer.
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
	// ReasonFloor, ReasonLoad and ReasonBuild are a resume or purchase for
	// warm slots alone: the market's floor, its share of load and
	// arrivals, or a recent build's shape.
	ReasonFloor ActionReason = "floor"
	ReasonLoad  ActionReason = "load"
	ReasonBuild ActionReason = "build"
	// ReasonSpotWait is an on-demand purchase for Spot-tolerant GPU work
	// that waited the Spot wait while no Spot offer could host it.
	ReasonSpotWait ActionReason = "spot_wait"
	// ReasonReserve is a reserve purchase, or a return to the reserve, for
	// the stopped target; ReasonLargest for a reserve that fits the largest
	// shape; ReasonPeak a return of a host that slept before while the
	// reserves hold less than the peak's share.
	ReasonReserve ActionReason = "reserve"
	ReasonLargest ActionReason = "largest"
	ReasonPeak    ActionReason = "peak"
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
	case ReasonBuild:
		return "a recent build's shape"
	case ReasonSpotWait:
		return "Spot refused past the Spot wait"
	case ReasonReserve:
		return "the stopped target"
	case ReasonLargest:
		return "a reserve that fits the largest shape"
	case ReasonPeak:
		return "the reserve under the peak's share"
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
	Reason CapacityWait
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

// PlanFleet decides one pass over a snapshot. On-demand hosts first decide
// whether they lend to Spot-tolerant work. One cover then packs pending
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
		hostRoom: s.HostRoom, reserveRoom: s.ReserveRoom, offers: map[string]ranking{}, byMarket: map[ReserveMarket]*marketView{},
		claimed: map[HostID]bool{}, holding: map[HostID][]warmSlot{}, plan: FleetPlan{IdleSince: map[HostID]time.Time{}},
		quotaUsed: QuotaUse(s.Hosts, s.Offers.Catalog), spot: freshSpot(p, s.Offers.Spot, s.Now),
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
	offers                    map[string]ranking
	// classReads is the largest read pending work of each demand class
	// makes, by class key.
	classReads map[string]int64
	// spotOffers holds spotOffered's answers.
	spotOffers map[spotNeed]bool
	byMarket   map[ReserveMarket]*marketView
	// claimed are hosts pending work or a warm slot fits, serving or
	// starting; holding are the warm slots each serving host keeps room
	// for.
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

// marketView is one market during the pass: its target, and what the pass
// decided for it.
type marketView struct {
	marketTarget
	retired map[HostID]bool
	// shortfall and stoppedShort are what the pass could not cover, and
	// warmReason and stoppedReason why.
	shortfall, stoppedShort   FleetCapacity
	warmReason, stoppedReason CapacityWait
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
	since  time.Time
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

// slotNeed is a warm slot of market m as a requirement; a GPU market's
// slot holds at least one card.
func slotNeed(m ReserveMarket, shape FleetCapacity) Requirement {
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
// region saves. A reserve there holds none of the stopped target, so the
// market buys one in the read region and retires it once that is ready; a
// host there does not return to the reserve.
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
	ps.planned = append(ps.planned, plannedHost{market: reserveMarketOf(o.Market, o.Type.GPU), offer: o, reserve: reserve})
	ps.start(o.Region, o.Type.Name, o.Market)
	return ps.act(FleetAction{Kind: kind, Reason: reason, Market: m, Host: replaces, Offer: &o, Mode: mode, Containers: containers, Holds: holds})
}

func (ps *pass) resume(m ReserveMarket, h *FleetHost, reason ActionReason, containers []uuid.UUID) int {
	// A stopping reserve holds its host room and quota already.
	if h.State != FleetStopping {
		ps.hostRoom--
		ps.start(h.Region, h.InstanceType, h.Market)
	}
	h.State = FleetStarting
	ps.reserveRoom++
	return ps.act(FleetAction{Kind: ActionResume, Reason: reason, Market: m, Host: ptr(h.ID), Containers: containers})
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
		rooms[i] = forWork(*bins[i].host, bins[i].room)
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

// binRooms are the bins' room for pending work.
func binRooms(bins []bin) []HostCapacity {
	rooms := make([]HostCapacity, len(bins))
	for i, b := range bins {
		rooms[i] = forWork(*b.host, b.room)
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
			if !ps.stalled(*h) {
				coming = append(coming, bin{host: h, room: h.capacity()})
			}
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
				comingRooms[i] = forWork(*coming[i].host, coming[i].room)
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
		if i := take(coming, comingRooms, booting, floor); i >= 0 {
			ps.claimed[coming[i].host.ID] = true
			ps.provisioning(it, ptr(coming[i].host.ID), nil)
			continue
		}
		left = append(left, it)
	}
	ps.buyFor(ps.resumeFor(left))
	// A purchase EC2 has not launched yet that nothing waits for any longer,
	// as when its container stopped, is not launched; a rightsize's waits
	// for its slots to move.
	for _, h := range ps.hosts {
		if h.Requested && !h.reserve() && h.Replaces == nil && !ps.claimed[h.ID] {
			ps.act(FleetAction{Kind: ActionCancel, Reason: ReasonUnclaimed, Market: h.market(), Host: ptr(h.ID)})
		}
	}
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

// reasonFor is why a resume or purchase takes items: pending work, or else
// the kind of the first warm slot.
func reasonFor(items []coverItem) ActionReason {
	if slices.ContainsFunc(items, func(it coverItem) bool { return !it.slot }) {
		return ReasonWork
	}
	switch items[0].kind {
	case slotFloor:
		return ReasonFloor
	case slotBuild:
		return ReasonBuild
	case slotLoad:
	}
	return ReasonLoad
}

// waits records why a pending container no purchase took waits.
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
// class makes there, so it shares purchases and warm slots with the rest of
// the class.
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
		took := make([]coverItem, len(taken))
		for i, n := range taken {
			took[i] = items[n]
			if !items[n].slot {
				ids = append(ids, items[n].id)
			}
		}
		ps.resume(items[taken[0]].market, h, reasonFor(took), ids)
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
// first item's market first, then one on the target agent build, then a
// stopped one, then the one that takes
// the most pending containers, then the one whose region they read least
// across, then one that takes every pending container and every slot of
// its market, so one resume does; then the smallest, the cheapest, and the
// one that takes the most slots.
// Spot work takes an on-demand reserve only while no Spot offer can host
// it, and a CPU one only while it is lendable. Without work, only slots of
// markets where no work waits, or slots that come with the work, take a
// reserve, and only one whose absence leaves a reserve that fits the
// largest shape and that costs no more to serve than buying for the slots
// it takes.
func (ps *pass) bestReserve(items []coverItem, work bool) (*FleetHost, []int) {
	var best *FleetHost
	var bestTaken []int
	var bestCount [2]int
	var bestRead int64
	all := func(h *FleetHost, taken []int) bool {
		for n, it := range items {
			if (!it.slot || it.market == h.market()) && !slices.Contains(taken, n) {
				return false
			}
		}
		return true
	}
	spotWork := slices.ContainsFunc(items, func(it coverItem) bool { return !it.slot && it.need.Preemptible })
	for i := range ps.hosts {
		h := &ps.hosts[i]
		if !ps.ready(*h, work) {
			continue
		}
		onDemand := h.Market == MarketOnDemand
		lendsCPU := spotWork && onDemand && h.GPU == "" && ps.lendable(*h)
		lends := func(need Requirement) bool { return !onDemand || (lendsCPU || h.GPU != "") && !ps.spotOffered(need) }
		b := bin{host: h, room: h.capacity()}
		var taken []int
		var count [2]int
		for kind, slot := range []bool{false, true} {
			for n, it := range items {
				eligible := it.slot == slot && ps.room(it.market) > 0 && b.takes(it)
				switch {
				case !eligible:
					continue
				case !slot && (!work || it.need.Preemptible && !lends(it.need)):
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
		var read int64
		for _, n := range taken {
			read += readMicros(ps.p, items[n].need.Read, h.Region)
		}
		if best == nil || cmp.Or(boolOrder(h.market() != items[taken[0]].market, best.market() != items[bestTaken[0]].market),
			boolOrder(!h.Current, !best.Current), boolOrder(h.State == FleetStopping, best.State == FleetStopping), cmp.Compare(bestCount[0], count[0]), cmp.Compare(read, bestRead),
			boolOrder(!all(h, taken), !all(best, bestTaken)), shapeOrder(h.Usable, best.Usable), ps.retention(h, best)) < 0 {
			best, bestTaken, bestCount, bestRead = h, taken, count, read
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

// resumePays reports whether resuming reserve h for the slots it takes, by
// index into items, costs no more over the cost horizon than the cheapest
// purchase that covers them. The resume is priced without the root it pays
// stopped as well, the purchase with what it runs while it provisions.
// With no purchase that covers them, a resume is the only way.
func (ps *pass) resumePays(h *FleetHost, items []coverItem, taken []int) bool {
	var need CoverNeed
	for _, n := range taken {
		shape, disks := reservedShape(items[n].need), items[n].need.Disks
		if i := slices.IndexFunc(need.Items, func(c CoverItem) bool { return c.Shape == shape && c.Disks == disks }); i >= 0 {
			need.Items[i].Count++
			continue
		}
		need.Items = append(need.Items, CoverItem{Shape: shape, Disks: disks, Count: 1})
	}
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
	return ps.floor(v, h.ID, FleetHost.resumable).Covers(v.stopped) && ps.leavesLarge(h)
}

// demandClass is cover items that can share offers, by shape and kind.
type demandClass struct {
	need   Requirement
	key    string
	market ReserveMarket
	groups []itemGroup
}

// itemGroup is a class's items of one shape, disk count and kind.
type itemGroup struct {
	shape FleetCapacity
	disks int
	slot  bool
	kind  slotKind
	items []coverItem
}

// coverNeed is what is left of the class's groups, of those only keeps
// when it is set, and the index of each need item's group. A warm slot of
// a market where work waits rides, taking only room left on hosts bought
// for that work, unless it comes with the work.
func (ps *pass) coverNeed(c *demandClass, only func(itemGroup) bool) (CoverNeed, []int) {
	var need CoverNeed
	var at []int
	for i, g := range c.groups {
		if len(g.items) == 0 || only != nil && !only(g) {
			continue
		}
		item := CoverItem{Shape: g.shape, Disks: g.disks, Count: len(g.items)}
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
		need, key := ps.classKey(it.need)
		n := slices.IndexFunc(classes, func(c *demandClass) bool { return c.key == key })
		if n < 0 {
			classes = append(classes, &demandClass{need: need, key: key, market: it.market})
			n = len(classes) - 1
		}
		c := classes[n]
		shape := reservedShape(it.need)
		i := slices.IndexFunc(c.groups, func(g itemGroup) bool {
			return g.shape == shape && g.disks == it.need.Disks && g.slot == it.slot && g.kind == it.kind
		})
		if i < 0 {
			c.groups = append(c.groups, itemGroup{shape: shape, disks: it.need.Disks, slot: it.slot, kind: it.kind})
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
				if it.slot {
					v := ps.byMarket[it.market]
					v.shortfall = v.shortfall.Plus(g.shape)
					switch {
					case v.warmReason != WaitNone:
					case ps.waiting[it.market]:
						v.warmReason = WaitDemand
					default:
						v.warmReason = ps.shortReason(it.market, slotNeed(it.market, g.shape), false)
					}
					continue
				}
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

// buyClass covers what is left of class c from offers: pending containers
// and warm slots on the same new hosts. The CPU Spot market buys Spot hosts
// that each hold at least its warm target, up to the largest shape's cap:
// Spot costs less per core on larger hosts, so a growing market buys hosts
// that grow with it, and the room they leave is headroom. What those
// cannot take, under a quota or a cooldown, any offer covers. On-demand
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
		least := v.warm.Lower(ps.p.LargestShape.Cap)
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
func (ps *pass) buyAtLoss(c *demandClass) {
	waited := func(it coverItem) bool {
		return !it.slot && ps.s.Now.Sub(it.since) >= ps.p.SpotWait && !ps.spotOffered(it.need)
	}
	if !slices.ContainsFunc(c.groups, func(g itemGroup) bool { return slices.ContainsFunc(g.items, waited) }) {
		return
	}
	in := ps.s.Offers
	in.WaiveMargin = true
	r := ranking{offers: RankOffers(ps.p, c.need, false, in), refs: sizeRefs(ps.p, c.need, false, in)}
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
	need, at := ps.coverNeed(c, only)
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
		var ids []uuid.UUID
		var holds FleetCapacity
		for _, it := range taken {
			holds = holds.Plus(reservedShape(it.need))
			if !it.slot {
				ids = append(ids, it.id)
			}
		}
		reason := reasonFor(taken)
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

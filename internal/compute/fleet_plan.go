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
	// Load is what its live containers reserve.
	Load       FleetCapacity
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
	// IdleSince is when the serving host became idle, as last recorded.
	IdleSince *time.Time
	// PhaseAt is when the host entered its phase; a serving host's is when
	// it became ready.
	PhaseAt time.Time
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

func (h FleetHost) hourly() int64 {
	if h.HourlyMicros == nil {
		return 0
	}
	return *h.HourlyMicros
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
	// within the policy's LargestShape window.
	Recent map[ReserveMarket]FleetCapacity
	Offers OfferInputs
	// HostRoom is how many more hosts may run or start, reserves being
	// prepared among them; ReserveRoom how many more may be held stopped.
	HostRoom    int
	ReserveRoom int
	// FloorShortSince is when each market's stopped floor went short, from
	// the last published plan.
	FloorShortSince map[ReserveMarket]time.Time
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
	// ActionRefresh resumes a reserve prepared for an older agent so it
	// updates and stops again.
	ActionRefresh FleetActionKind = "refresh"
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
	// ReasonDemand holds headroom growth while pending work waits.
	ReasonDemand MarketReason = "waiting for demand"
	// ReasonActionCap defers the rest of the growth to the next pass.
	ReasonActionCap MarketReason = "growth continues next pass"
	// ReasonFleetLimit means the fleet's host limit holds growth back.
	ReasonFleetLimit MarketReason = "fleet host limit reached"
	// ReasonNoOffer means no approved offer covers the shortfall.
	ReasonNoOffer MarketReason = "no approved offer"
)

// MarketPlan is one market's targets and measures.
type MarketPlan struct {
	Market ReserveMarket
	// Load is what the market's containers hold now: running and pending.
	Load                              FleetCapacity
	WarmTarget, WarmFree, WarmPending FleetCapacity
	StoppedTarget                     FleetCapacity
	ReserveReady, ReservePending      FleetCapacity
	Shortfall, StoppedShortfall       FleetCapacity
	// FloorShortSince is when the stopped floor went short; nil while held.
	FloorShortSince *time.Time
	Reason          MarketReason
	States          []FleetStateCapacity
}

// FleetPlan is one pass's decisions.
type FleetPlan struct {
	Markets []MarketPlan
	Actions []FleetAction
	Waits   []ContainerWait
	// IdleSince is when each idle serving host became idle.
	IdleSince map[HostID]time.Time
}

// PlanFleet decides one pass over a snapshot. Pending demand goes on ready
// room, then starting hosts, then resumed reserves, then purchases. Then, in
// each market, resumes and purchases keep the warm target while no work
// waits. Idle hosts beyond it leave, into the reserve while the reserve is
// short. The pass buys reserves up to the stopped target or retires the
// surplus, rightsizes an idle host a cheaper type could replace, and
// refreshes stale reserves.
func PlanFleet(p Policy, s FleetSnapshot) FleetPlan {
	ps := &pass{
		p: p, s: s, hosts: slices.Clone(s.Hosts),
		used: map[ReserveMarket]int{}, waiting: map[ReserveMarket]bool{}, limited: map[ReserveMarket]bool{},
		hostRoom: s.HostRoom, reserveRoom: s.ReserveRoom, offers: map[string][]FleetOffer{}, byMarket: map[ReserveMarket]*marketView{},
		claimed: map[HostID]bool{}, plan: FleetPlan{IdleSince: map[HostID]time.Time{}}, quotaUsed: QuotaUse(s.Hosts, s.Offers.Catalog),
	}
	ps.s.Offers.QuotaUsed = maps.Clone(ps.quotaUsed)
	items := ps.pendingItems()
	views := ps.views(items)
	ps.demand(items)
	ps.idle()
	for _, v := range views {
		v.mayGrow = !ps.waiting[v.m]
		ps.grow(v)
		ps.retain(v)
		ps.reserves(v)
		ps.rightsize(v)
		ps.refresh(v)
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
	planned               []plannedHost
	used                  map[ReserveMarket]int
	waiting, limited      map[ReserveMarket]bool
	hostRoom, reserveRoom int
	offers                map[string][]FleetOffer
	byMarket              map[ReserveMarket]*marketView
	// claimed are serving hosts pending work fits.
	claimed map[HostID]bool
	// quotaUsed is what running hosts and this pass's starts count
	// against each vCPU quota.
	quotaUsed map[QuotaKey]int64
}

type plannedHost struct {
	market  ReserveMarket
	offer   FleetOffer
	reserve bool
}

// marketView is one market during the pass.
type marketView struct {
	m             ReserveMarket
	load          FleetCapacity
	warm, stopped FleetCapacity
	// largest is the shape one of the market's reserves fits; empty for
	// none. apart keeps that reserve beside the stopped target.
	largest FleetCapacity
	apart   bool
	mayGrow bool
	retired map[HostID]bool
	// shortfall and stoppedShort are what the pass could not cover.
	shortfall, stoppedShort FleetCapacity
	// floorShortSince is when the stopped floor went short; nil while held.
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

// pendingItem is one pending container during demand placement.
type pendingItem struct {
	id     uuid.UUID
	need   Requirement
	market ReserveMarket
	bought *HostID
	wait   int
}

// pendingItems lists the pending containers, each with a wait slot and the
// market its demand belongs to: those a host was bought for first, so other
// work never takes their room, then largest first.
func (ps *pass) pendingItems() []pendingItem {
	var items []pendingItem
	for _, g := range ps.s.Pending {
		m := ps.demandMarket(g.Need)
		for _, c := range g.Containers {
			ps.plan.Waits = append(ps.plan.Waits, ContainerWait{Container: c.ID})
			items = append(items, pendingItem{id: c.ID, need: g.Need, market: m, bought: c.Host, wait: len(ps.plan.Waits) - 1})
		}
	}
	slices.SortStableFunc(items, func(a, b pendingItem) int {
		return cmp.Or(boolOrder(a.bought == nil, b.bought == nil), size(b.need)-size(a.need))
	})
	return items
}

// views sets every market's targets from its current load: what its hosts'
// containers reserve and what its pending containers ask for.
func (ps *pass) views(items []pendingItem) []*marketView {
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
		v.load = totalOf(ps.inMarket(m, func(FleetHost) bool { return true }), func(h *FleetHost) FleetCapacity { return h.Load })
		largest := ps.s.Recent[m]
		for _, it := range items {
			if it.market == m {
				v.load = v.load.Plus(reservedShape(it.need))
				largest = largest.Upper(reservedShape(it.need))
			}
		}
		r := ps.p.Reserve(m)
		v.warm, v.stopped = r.Warm.Of(v.load), r.Stopped.Of(v.load)
		switch r.Largest {
		case LargestApart, LargestShared:
			v.largest, v.apart = ps.p.LargestShape.of(m, largest), r.Largest == LargestApart
		case LargestNone:
		}
		ps.byMarket[m] = v
		views = append(views, v)
	}
	return views
}

// reservedShape is what a requirement reserves on a host.
func reservedShape(r Requirement) FleetCapacity {
	return FleetCapacity{CPUMillis: r.CPUMillis, MemoryBytes: r.MemoryBytes, GPUs: r.GPUsNeeded()}
}

// marketOffers are the offers a market's own growth buys from.
func (ps *pass) marketOffers(m ReserveMarket, reserve bool) []FleetOffer {
	need := Requirement{Preemptible: m.Preemptible}
	if m.GPU != "" {
		need.GPUs = []string{m.GPU}
	}
	key := m.String() + "/" + boolKey(reserve)
	if offers, ok := ps.offers[key]; ok {
		return offers
	}
	offers := slices.DeleteFunc(RankOffers(ps.p, need, reserve, ps.s.Offers), func(o FleetOffer) bool {
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
	return rootDiskMicros(h.Region, t.RootGiB(h.ReserveMode != nil && *h.ReserveMode == ReserveHibernate))
}

func (ps *pass) cooled(h FleetHost) bool {
	return cooled(ps.s.Offers.Cooldowns, ps.s.Now, h.Region, h.InstanceType, h.Market)
}

func (ps *pass) room(m ReserveMarket) int { return ps.p.MaxGrowthActions - ps.used[m] }

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
	case ActionResume, ActionBuy, ActionBuyReserve, ActionRightsize, ActionRefresh:
		ps.used[a.Market]++
	case ActionReturnToReserve, ActionDrain, ActionRetireReserve:
	}
	ps.plan.Actions = append(ps.plan.Actions, a)
	return len(ps.plan.Actions) - 1
}

// buy launches a host from o, to serve or as a reserve in its sleep mode.
func (ps *pass) buy(m ReserveMarket, o FleetOffer, kind FleetActionKind, containers []uuid.UUID, replaces *HostID) int {
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
	ps.planned = append(ps.planned, plannedHost{market: m, offer: o, reserve: reserve})
	ps.start(o.Region, o.Type.Name, o.Market)
	return ps.act(FleetAction{Kind: kind, Market: m, Host: replaces, Offer: &o, Mode: mode, Containers: containers})
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

// fitFirst reserves r on the first host that fits it and returns its index,
// or -1.
func fitFirst(hosts []HostCapacity, r Requirement) int {
	for i := range hosts {
		if hosts[i].Fits(r) {
			hosts[i].Reserve(r)
			return i
		}
	}
	return -1
}

// demand places pending containers: ready room, then the host bought for
// one, then starting hosts, then ready reserves resumed for them, then new
// hosts covering the rest.
func (ps *pass) demand(items []pendingItem) {
	var ready, coming []HostCapacity
	var readyAt []int
	for i, h := range ps.hosts {
		if h.State == FleetServing {
			ready, readyAt = append(ready, h.capacity()), append(readyAt, i)
		}
		if h.State == FleetStarting {
			coming = append(coming, h.capacity())
		}
	}
	var unplaced []pendingItem
	for _, it := range items {
		if it.need.Machine != "" || it.need.Connection != nil {
			continue
		}
		if i := fitFirst(ready, it.need); i >= 0 {
			// The room it takes is load, not warm headroom.
			h := &ps.hosts[readyAt[i]]
			h.Load = h.Load.Plus(reservedShape(it.need))
			ps.claimed[h.ID] = true
			continue
		}
		ps.waiting[it.market] = true
		if it.bought != nil {
			if i := slices.IndexFunc(coming, func(h HostCapacity) bool { return h.Host == *it.bought }); i >= 0 {
				coming[i].Reserve(it.need)
				ps.provisioning(it, &coming[i].Host, nil)
				continue
			}
		}
		if i := fitFirst(coming, it.need); i >= 0 {
			ps.provisioning(it, &coming[i].Host, nil)
			continue
		}
		unplaced = append(unplaced, it)
	}
	ps.buyFor(ps.resumeFor(unplaced))
}

func (ps *pass) provisioning(it pendingItem, host *HostID, action *int) {
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

// resumeFor resumes ready reserves for containers nothing running takes:
// the container's own market first, then the cheapest. Spot work borrows an
// on-demand reserve only while the on-demand reserves left behind meet
// their stopped target.
func (ps *pass) resumeFor(items []pendingItem) []pendingItem {
	type resumed struct {
		action int
		host   HostCapacity
	}
	var opened []resumed
	var left []pendingItem
	for _, it := range items {
		if i := slices.IndexFunc(opened, func(r resumed) bool { return r.host.Fits(it.need) }); i >= 0 {
			opened[i].host.Reserve(it.need)
			a := &ps.plan.Actions[opened[i].action]
			a.Containers = append(a.Containers, it.id)
			ps.provisioning(it, &opened[i].host.Host, nil)
			continue
		}
		h := ps.reserveFor(it.need, it.market)
		if h == nil || ps.hostRoom <= 0 || ps.room(it.market) <= 0 {
			left = append(left, it)
			continue
		}
		capacity := h.capacity()
		capacity.Reserve(it.need)
		opened = append(opened, resumed{action: ps.resume(it.market, h, ActionResume, []uuid.UUID{it.id}), host: capacity})
		ps.provisioning(it, &opened[len(opened)-1].host.Host, nil)
	}
	return left
}

// ready is a stopped reserve that can resume now. One prepared for an older
// agent release serves too: its session updates the agent in place.
func (ps *pass) ready(h FleetHost) bool {
	return h.resumable() && !h.Protected && !ps.cooled(h) && ps.startable(h)
}

// reserveFor is the ready reserve to resume for work of market m. It
// prefers one whose resume leaves a reserve that fits the largest shape,
// then one of m, then the smallest, then the cheapest.
func (ps *pass) reserveFor(need Requirement, m ReserveMarket) *FleetHost {
	var candidates []*FleetHost
	takesLarge := map[HostID]bool{}
	for i := range ps.hosts {
		h := &ps.hosts[i]
		if !ps.ready(*h) || !h.capacity().Fits(need) || (need.Preemptible && h.Market == MarketOnDemand && !ps.lendable(*h)) {
			continue
		}
		candidates = append(candidates, h)
		takesLarge[h.ID] = !ps.leavesLarge(*h)
	}
	if len(candidates) == 0 {
		return nil
	}
	return slices.MinFunc(candidates, func(a, b *FleetHost) int {
		return cmp.Or(boolOrder(takesLarge[a.ID], takesLarge[b.ID]), boolOrder(a.market() != m, b.market() != m),
			shapeOrder(a.Usable, b.Usable), cheaper(a, b))
	})
}

// leavesLarge reports whether resuming reserve h leaves a reserve that fits
// the largest shape, where h fits it and the market keeps one apart.
func (ps *pass) leavesLarge(h FleetHost) bool {
	v := ps.byMarket[h.market()]
	return !v.apart || !h.Usable.Covers(v.largest) || ps.holdsLargest(v, h.ID)
}

// cheaper orders hosts by known hourly cost, unknown last.
func cheaper(a, b *FleetHost) int {
	return cmp.Or(boolOrder(a.HourlyMicros == nil, b.HourlyMicros == nil), cmp.Compare(a.hourly(), b.hourly()),
		strings.Compare(a.ID.String(), b.ID.String()))
}

// lendable reports whether Spot work may resume on-demand reserve h: the
// ready on-demand reserves left without it still meet their stopped target.
func (ps *pass) lendable(h FleetHost) bool {
	v := ps.byMarket[h.market()]
	return ps.floor(v, h.ID, func(r FleetHost) bool { return r.resumable() && !r.Protected }).Covers(v.stopped)
}

// demandClass is containers that can share offers.
type demandClass struct {
	need       Requirement
	market     ReserveMarket
	shapes     []FleetCapacity
	containers [][]pendingItem
}

// buyFor covers the remaining containers with new hosts, per class of
// containers that share offers.
func (ps *pass) buyFor(items []pendingItem) {
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
		i := slices.Index(c.shapes, shape)
		if i < 0 {
			c.shapes = append(c.shapes, shape)
			c.containers = append(c.containers, nil)
			i = len(c.shapes) - 1
		}
		c.containers[i] = append(c.containers[i], it)
	}
	for _, c := range classes {
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
		if ps.hostRoom <= 0 {
			for _, left := range c.containers {
				for _, it := range left {
					ps.plan.Waits[it.wait].Wait = ptr(WaitLimit)
					ps.limited[c.market] = true
				}
			}
		}
	}
}

// buyClass covers what is left of a class with the offers of market m.
func (ps *pass) buyClass(c *demandClass, m ReserveMarket, offers []FleetOffer) {
	if m.GPU != "" {
		offers = slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool { return o.Type.GPU != m.GPU })
	}
	need := CoverNeed{Items: make([]CoverItem, len(c.shapes))}
	left := 0
	for i, shape := range c.shapes {
		need.Items[i] = CoverItem{Shape: shape, Count: len(c.containers[i])}
		left += len(c.containers[i])
	}
	if left == 0 {
		return
	}
	result := Cover(preferHealthy(offers), need, servingCost(ps.p), ps.limits(min(ps.room(m), ps.hostRoom)))
	for _, node := range result.Nodes {
		var ids []uuid.UUID
		var taken []pendingItem
		for i, n := range node.Placed {
			taken = append(taken, c.containers[i][:n]...)
			c.containers[i] = c.containers[i][n:]
		}
		for _, it := range taken {
			ids = append(ids, it.id)
		}
		action := ps.buy(m, node.Offer, ActionBuy, ids, nil)
		for _, it := range taken {
			ps.provisioning(it, nil, ptr(action))
		}
	}
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

// grow keeps the warm target while no work waits: resume ready reserves
// that leave a large-shape reserve, cheapest first, then buy the cheapest
// cover of what is short.
func (ps *pass) grow(v *marketView) {
	if !v.mayGrow {
		return
	}
	short := v.warm.Minus(ps.warmFree(v)).Clamp().Minus(ps.warmPending(v)).Clamp()
	reserves := ps.inMarket(v.m, ps.ready)
	slices.SortStableFunc(reserves, cheaper)
	for _, h := range reserves {
		if short.Empty() || ps.room(v.m) <= 0 || ps.hostRoom <= 0 {
			break
		}
		if !ps.leavesLarge(*h) {
			continue
		}
		ps.resume(v.m, h, ActionResume, nil)
		short = short.Minus(h.Usable).Clamp()
	}
	if !short.Empty() {
		result := Cover(preferHealthy(ps.marketOffers(v.m, false)), CoverNeed{Aggregate: short}, servingCost(ps.p),
			ps.limits(min(ps.room(v.m), ps.hostRoom)))
		for _, node := range result.Nodes {
			ps.buy(v.m, node.Offer, ActionBuy, nil, nil)
		}
		short = short.Minus(result.Supplied).Clamp()
	}
	v.shortfall = short
}

// retain releases idle hosts the market no longer needs, most expensive
// first, while the free room left over the warm target still covers it.
func (ps *pass) retain(v *marketView) {
	surplus := ps.warmFree(v).Minus(v.warm)
	if !surplus.Covers(FleetCapacity{}) {
		return
	}
	idle := ps.inMarket(v.m, func(h FleetHost) bool { return serving(h) && ps.idleLong(h) })
	slices.SortStableFunc(idle, func(a, b *FleetHost) int {
		return cmp.Or(cmp.Compare(b.hourly(), a.hourly()), cmp.Compare(b.Usable.CPUMillis, a.Usable.CPUMillis),
			cmp.Compare(b.Usable.MemoryBytes, a.Usable.MemoryBytes), strings.Compare(a.ID.String(), b.ID.String()))
	})
	for _, h := range idle {
		if surplus.Minus(h.Usable).Covers(FleetCapacity{}) {
			surplus = surplus.Minus(h.Usable)
			ps.leave(v, h)
		}
	}
}

// leave returns a leaving host to the reserve when EC2 can stop it and the
// market's reserve falls short, or holds nothing that fits the largest shape
// while the host does. A host launched able to hibernate does where
// hibernates allows. Any other leaving host drains.
func (ps *pass) leave(v *marketView, h *FleetHost) {
	t, catalogued := ps.typeNamed(h.InstanceType)
	short := !ps.floor(v, HostID{}, FleetHost.reserve).Covers(v.stopped) || (h.Usable.Covers(v.largest) && !ps.holdsLargest(v, HostID{}))
	if ps.reserveRoom > 0 && catalogued && h.Stoppable && short {
		mode := ReserveStop
		if h.HibernationConfigured && hibernates(t, h.Market) {
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
// selects, less skip, retired ones and the large-shape reserve kept apart.
func (ps *pass) floor(v *marketView, skip HostID, keep func(FleetHost) bool) FleetCapacity {
	large := ps.largeReserve(v, skip)
	return totalOf(ps.inMarket(v.m, func(h FleetHost) bool {
		return h.reserve() && keep(h) && h.ID != skip && !v.retired[h.ID] && (large == nil || h.ID != large.ID)
	}), func(h *FleetHost) FleetCapacity { return h.Usable })
}

// largeReserve is the reserve a market keeps apart for its largest shape:
// the smallest, then cheapest, that fits it, not retired and other than
// skip. It is nil when the market keeps none apart or no reserve fits.
func (ps *pass) largeReserve(v *marketView, skip HostID) *FleetHost {
	if !v.apart || v.largest.Empty() {
		return nil
	}
	var large *FleetHost
	for _, h := range ps.inMarket(v.m, FleetHost.reserve) {
		if h.ID != skip && !v.retired[h.ID] && h.Usable.Covers(v.largest) &&
			(large == nil || cmp.Or(shapeOrder(h.Usable, large.Usable), cheaper(h, large)) < 0) {
			large = h
		}
	}
	return large
}

// largeAway reports whether a host of the market that fits the largest
// shape may still return to the reserve: EC2 can stop it, and it starts,
// idles or became ready within the idle timeout.
func (ps *pass) largeAway(v *marketView) bool {
	return len(ps.inMarket(v.m, func(h FleetHost) bool {
		_, idle := ps.plan.IdleSince[h.ID]
		return h.Usable.Covers(v.largest) && h.Stoppable && !h.Protected &&
			(starting(h) || serving(h) && (idle || ps.s.Now.Sub(h.PhaseAt) < ps.p.IdleTimeout))
	})) > 0
}

// holdsLargest reports whether a reserve of the market other than skip, or
// one this pass buys, fits the market's largest shape.
func (ps *pass) holdsLargest(v *marketView, skip HostID) bool {
	if v.largest.Empty() {
		return true
	}
	for _, h := range ps.inMarket(v.m, FleetHost.reserve) {
		if h.ID != skip && !v.retired[h.ID] && h.Usable.Covers(v.largest) {
			return true
		}
	}
	return slices.ContainsFunc(ps.planned, func(n plannedHost) bool {
		return n.market == v.m && n.reserve && n.offer.Usable.Covers(v.largest)
	})
}

// While no work waits, reserves buys the stopped target's shortfall and,
// when no reserve fits the largest shape, one that does. A shared one counts
// toward the target. One kept apart is bought on its own, and not while a
// host that fits the shape may still return to the reserve. With the target
// held, it retires the reserves the market no longer needs.
func (ps *pass) reserves(v *marketView) {
	short := v.stopped.Minus(ps.floor(v, HostID{}, FleetHost.reserve)).Clamp()
	v.stoppedShort = short
	floorDue := ps.floorDue(v, short)
	largest := CoverNeed{Items: []CoverItem{{Shape: v.largest, Count: 1}}}
	switch held := ps.holdsLargest(v, HostID{}); {
	case !v.mayGrow:
	case v.apart:
		if !short.Empty() && floorDue {
			v.stoppedShort = short.Minus(ps.buyReserves(v, CoverNeed{Aggregate: short}, short).Supplied).Clamp()
			if v.stoppedShort.Empty() {
				v.floorShortSince = nil
			}
		}
		if !held && !ps.largeAway(v) && len(ps.buyReserves(v, largest, v.largest).UnmetItems) > 0 {
			v.stoppedShort = v.stoppedShort.Upper(v.largest)
		}
	case !held:
		largest.Aggregate = short.Minus(v.largest).Clamp()
		result := ps.buyReserves(v, largest, v.largest)
		v.stoppedShort = largest.Aggregate.Minus(result.Supplied).Clamp()
		if len(result.UnmetItems) > 0 {
			v.stoppedShort = short.Minus(result.Supplied).Clamp().Upper(v.largest)
		}
	case !short.Empty():
		v.stoppedShort = short.Minus(ps.buyReserves(v, CoverNeed{Aggregate: short}, FleetCapacity{}).Supplied).Clamp()
	}
	if short.Empty() {
		ps.retire(v)
	}
}

// floorDue records when market v's stopped floor went short and reports
// whether to buy for it: once it has been short for the idle timeout, so a
// reserve resumed for a burst can return before a replacement is bought.
func (ps *pass) floorDue(v *marketView, short FleetCapacity) bool {
	if short.Empty() {
		return false
	}
	since, ok := ps.s.FloorShortSince[v.m]
	if !ok {
		since = ps.s.Now
	}
	v.floorShortSince = &since
	return ps.s.Now.Sub(since) >= ps.p.IdleTimeout
}

// buyReserves buys the cheapest reserves that cover need, ones that
// hibernate wherever one fits shape.
func (ps *pass) buyReserves(v *marketView, need CoverNeed, shape FleetCapacity) CoverResult {
	offers := preferHealthy(ps.marketOffers(v.m, true))
	if !shape.Empty() {
		offers = preferHibernating(offers, shape)
	}
	result := Cover(offers, need, reserveCost(ps.p), ps.limits(min(ps.room(v.m), ps.reserveRoom, ps.hostRoom)))
	for _, node := range result.Nodes {
		ps.buy(v.m, node.Offer, ActionBuyReserve, nil, nil)
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
// cannot buy again first, then ones not ready, then ones prepared for an
// older agent release, then the costliest to hold.
// It keeps the ready capacity the target needs, so a pending reserve never
// stands in for a ready one, and a reserve that fits the largest shape.
func (ps *pass) retire(v *marketView) {
	ready := FleetHost.resumable
	needReady := ps.floor(v, HostID{}, ready).Lower(v.stopped)
	growable := func(h *FleetHost) bool {
		_, ok := ps.typeNamed(h.InstanceType)
		return ok && !ps.cooled(*h)
	}
	candidates := ps.inMarket(v.m, func(h FleetHost) bool { return h.reserve() && !h.Protected && h.State != FleetStopping })
	slices.SortStableFunc(candidates, func(a, b *FleetHost) int {
		return cmp.Or(boolOrder(growable(a), growable(b)), boolOrder(ready(*a), ready(*b)), boolOrder(a.Current, b.Current),
			cmp.Compare(ps.stoppedMicros(*b), ps.stoppedMicros(*a)), cmp.Compare(b.Usable.CPUMillis, a.Usable.CPUMillis),
			cmp.Compare(b.Usable.MemoryBytes, a.Usable.MemoryBytes), strings.Compare(a.ID.String(), b.ID.String()))
	})
	for _, h := range candidates {
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
// while it provisions. It replaces only an idle host the warm target needs,
// and only while nothing else in the market grows, starts or leaves, so the
// warm target converges on the cheapest type. The same type in another zone
// never replaces it, so a Spot price move between zones buys nothing.
func (ps *pass) rightsize(v *marketView) {
	moving := slices.ContainsFunc(ps.plan.Actions, func(a FleetAction) bool {
		return a.Market == v.m && a.Kind != ActionRetireReserve
	})
	if !v.mayGrow || moving || !ps.warmPending(v).Empty() || ps.room(v.m) <= 0 || ps.hostRoom <= 0 {
		return
	}
	hosts := ps.inMarket(v.m, serving)
	var best *FleetOffer
	var replaced *FleetHost
	var bestPayback int64
	horizon, provision := int64(ps.p.CostHorizon/time.Second), int64(ps.p.Provision/time.Second)
	for _, h := range hosts {
		if !ps.idleLong(*h) || h.HourlyMicros == nil {
			continue
		}
		others := totalOf(hosts, func(o *FleetHost) FleetCapacity {
			if o.ID == h.ID {
				return FleetCapacity{}
			}
			return o.free()
		})
		need := v.warm.Minus(others).Clamp()
		if need.Empty() {
			continue
		}
		for _, o := range ps.marketOffers(v.m, false) {
			payback := (*h.HourlyMicros-o.HourlyMicros)*horizon - o.HourlyMicros*provision
			if !o.CoolingRegion && o.Type.Name != h.InstanceType && o.Usable.Covers(need) && payback > bestPayback {
				best, replaced, bestPayback = &o, h, payback
			}
		}
	}
	if best != nil {
		ps.buy(v.m, *best, ActionRightsize, nil, ptr(replaced.ID))
	}
}

// refresh resumes a reserve prepared for an older agent release so it
// updates in place and stops again. A market refreshes one reserve at a
// time, and only while none of its reserves is being prepared or stopping,
// so the others stay ready. It picks first one whose absence leaves a
// reserve that fits the largest shape.
func (ps *pass) refresh(v *marketView) {
	if ps.room(v.m) <= 0 || ps.hostRoom <= 0 ||
		len(ps.inMarket(v.m, func(h FleetHost) bool { return h.reserve() && !h.resumable() && !v.retired[h.ID] })) > 0 ||
		slices.ContainsFunc(ps.planned, func(n plannedHost) bool { return n.market == v.m && n.reserve }) {
		return
	}
	stale := ps.inMarket(v.m, func(h FleetHost) bool { return ps.ready(h) && !h.Current && !v.retired[h.ID] })
	if len(stale) == 0 {
		return
	}
	h := slices.MinFunc(stale, func(a, b *FleetHost) int { return boolOrder(!ps.leavesLarge(*a), !ps.leavesLarge(*b)) })
	h.State = FleetPreparing
	ps.hostRoom--
	ps.start(h.Region, h.InstanceType, h.Market)
	ps.act(FleetAction{Kind: ActionRefresh, Market: v.m, Host: ptr(h.ID)})
}

// report is the market's published plan.
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
		Market: v.m, Load: v.load, WarmTarget: v.warm, WarmFree: ps.warmFree(v), WarmPending: ps.warmPending(v),
		StoppedTarget: v.stopped, ReserveReady: ready, ReservePending: reserve.Minus(ready).Clamp(),
		Shortfall: v.shortfall, StoppedShortfall: v.stoppedShort, FloorShortSince: v.floorShortSince, States: ps.states(v.m),
	}
	switch {
	case !v.mayGrow:
		plan.Reason = ReasonDemand
	case plan.Shortfall.Empty() && plan.StoppedShortfall.Empty():
	case ps.room(v.m) <= 0:
		plan.Reason = ReasonActionCap
	case ps.hostRoom <= 0 || ps.reserveRoom <= 0 || ps.limited[v.m]:
		plan.Reason = ReasonFleetLimit
	default:
		plan.Reason = ReasonNoOffer
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

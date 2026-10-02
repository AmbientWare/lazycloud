package compute

import (
	"cmp"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
)

// ReserveMode is how a reserve stops.
type ReserveMode string

const (
	ReserveStop      ReserveMode = "stop"
	ReserveHibernate ReserveMode = "hibernate"
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
	// Pinned counts live containers that cannot move: they did not accept
	// interruption or are bound to the host.
	Pinned int
	// Protected is an interruption's source or replacement.
	Protected bool
	// LaunchedAt starts the billing minimum; nil counts as settled.
	LaunchedAt *time.Time
	// Current is set on a reserve prepared for the current agent release.
	Current bool
	// ReserveMode is set on a reserve; nil on a serving host.
	ReserveMode *ReserveMode
	// HibernationConfigured is set when the instance launched able to
	// hibernate.
	HibernationConfigured bool
	// HourlyMicros is the complete hourly cost; nil when unknown.
	HourlyMicros *int64
	// LightSince is when the host became lightly used, as last published.
	LightSince *time.Time
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

func (h FleetHost) hibernating() bool {
	return h.ReserveMode != nil && *h.ReserveMode == ReserveHibernate
}

func (h FleetHost) settled(p Policy, now time.Time) bool {
	return h.LaunchedAt == nil || !now.Before(h.LaunchedAt.Add(p.BillingMinimum))
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

func lightlyUsed(load, capacity FleetCapacity, percent int64) bool {
	return load.CPUMillis*100 <= capacity.CPUMillis*percent && load.MemoryBytes*100 <= capacity.MemoryBytes*percent &&
		int64(load.GPUs)*100 <= int64(capacity.GPUs)*percent
}

// LocationDemand is expected demand pinned to a region or zone.
type LocationDemand struct {
	// Region is a product region; Zone a zone name or id. Empty is any.
	Region string
	Zone   string
	Shape  FleetCapacity
	Count  int
}

func (d LocationDemand) accepts(region, zone, zoneID string) bool {
	return (d.Region == "" || ProductRegion(region) == d.Region) && (d.Zone == "" || d.Zone == zone || d.Zone == zoneID)
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

// MarketRecord is a market's published consolidation state.
type MarketRecord struct {
	ConsolidatingHost    *HostID
	ConsolidationStarted *time.Time
	CooldownUntil        *time.Time
}

// FleetSnapshot is everything one planning pass reads.
type FleetSnapshot struct {
	Now       time.Time
	Hosts     []FleetHost
	Pending   []DemandGroup
	Forecasts map[ReserveMarket]MarketForecast
	Locations map[ReserveMarket][]LocationDemand
	Offers    OfferInputs
	Markets   map[ReserveMarket]MarketRecord
	// HostRoom is how many more hosts may run or start; ReserveRoom how
	// many more may be held stopped.
	HostRoom    int
	ReserveRoom int
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
	// ActionConsolidate moves a lightly used host's work to Destination.
	ActionConsolidate FleetActionKind = "consolidate"
	// ActionRightsize buys Offer to replace an idle Host; the host stays
	// until the replacement serves.
	ActionRightsize FleetActionKind = "rightsize"
	// ActionRefresh resumes a reserve prepared for an older agent so it
	// updates and stops again.
	ActionRefresh FleetActionKind = "refresh"
)

// FleetAction is one decision, naming a host or an offer.
type FleetAction struct {
	Kind        FleetActionKind
	Market      ReserveMarket
	Host        *HostID
	Offer       *FleetOffer
	Mode        *ReserveMode
	Destination *HostID
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
	// ReasonDemandOrRecovery holds elective growth while demand or an
	// interruption recovery is under way.
	ReasonDemandOrRecovery MarketReason = "waiting for demand or recovery"
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
	Load   FleetCapacity
	// Quiet is load within the warm floor.
	Quiet                                bool
	WarmTarget, WarmFree, WarmPending    FleetCapacity
	StoppedTarget                        FleetCapacity
	ReserveCapacity                      FleetCapacity
	ReserveReady, ReservePending         FleetCapacity
	HibernationTarget                    FleetCapacity
	Hibernated, HibernationUnverified    FleetCapacity
	Shortfall, StoppedShortfall          FleetCapacity
	HibernationShortfall                 FleetCapacity
	UnmetShapes, UnmetStoppedShapes      []FleetCapacity
	UnmetLocations                       []LocationDemand
	Reason                               MarketReason
	States                               []FleetStateCapacity
	Forecast                             *MarketForecast
	ConsolidationCandidate, Consolidates *HostID
}

// FleetPlan is one pass's decisions.
type FleetPlan struct {
	Markets []MarketPlan
	Actions []FleetAction
	Waits   []ContainerWait
	// LightSince is when each lightly used serving host became so.
	LightSince map[HostID]time.Time
}

// PlanFleet decides one pass over a snapshot. Per market, in order: targets;
// pending demand on ready room, then starting hosts, then resumed reserves,
// then purchases; elective growth when the market has no waiting demand and
// no recovery; consolidation; retention of idle hosts, returning a leaving
// host to the reserve while the reserve falls short; reserve growth or
// retirement; rightsizing; refresh of stale reserves.
func PlanFleet(p Policy, s FleetSnapshot) FleetPlan {
	ps := &pass{
		p: p, s: s, hosts: slices.Clone(s.Hosts),
		used: map[ReserveMarket]int{}, waiting: map[ReserveMarket]bool{}, limited: map[ReserveMarket]bool{},
		hostRoom: s.HostRoom, reserveRoom: s.ReserveRoom, offers: map[string][]FleetOffer{}, targets: map[ReserveMarket]MarketTargets{},
		plan: FleetPlan{LightSince: map[HostID]time.Time{}},
	}
	views := ps.views()
	ps.demand()
	ps.light()
	for _, v := range views {
		v.mayGrow = !ps.waiting[v.m] && !v.recovering
		ps.grow(v)
		ps.consolidate(v)
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
	targets               map[ReserveMarket]MarketTargets
}

type plannedHost struct {
	market  ReserveMarket
	offer   FleetOffer
	reserve bool
}

// marketView is one market during the pass.
type marketView struct {
	m          ReserveMarket
	t          MarketTargets
	load       FleetCapacity
	quiet      bool
	forecast   *MarketForecast
	shapes     []FleetCapacity
	locations  []LocationDemand
	recovering bool
	mayGrow    bool
	// protected are serving hosts that cover location demand, then
	// consolidation's destination.
	protected  map[HostID]bool
	moving     *HostID
	candidate  *HostID
	retired    map[HostID]bool
	shortfall  FleetCapacity
	unmet      []FleetCapacity
	stopped    FleetCapacity
	stoppedOut []FleetCapacity
	hibernate  FleetCapacity
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

// views sets every market's targets from the snapshot.
func (ps *pass) views() []*marketView {
	markets := ps.p.Markets()
	add := func(m ReserveMarket) {
		if !slices.Contains(markets, m) {
			markets = append(markets, m)
		}
	}
	for _, h := range ps.hosts {
		add(h.market())
	}
	for m := range ps.s.Forecasts {
		add(m)
	}
	slices.SortStableFunc(markets[len(ps.p.Markets()):], func(a, b ReserveMarket) int { return strings.Compare(a.String(), b.String()) })
	var views []*marketView
	for _, m := range markets {
		v := &marketView{m: m, protected: map[HostID]bool{}, retired: map[HostID]bool{}, locations: ps.s.Locations[m]}
		if f, ok := ps.s.Forecasts[m]; ok {
			v.forecast = &f
			v.shapes = f.Shapes
		}
		var running []FleetCapacity
		for _, h := range ps.inMarket(m, func(FleetHost) bool { return true }) {
			v.load = v.load.Plus(h.Load)
			if !h.reserve() {
				running = append(running, h.Load)
			}
			if h.Protected && h.State == FleetDraining {
				v.recovering = true
			}
		}
		v.t = TargetsFor(ps.p, m, TargetInputs{
			Load: v.load, Forecast: v.forecast, RunningLoads: running, HibernationShapes: ps.hibernationShapes(m), Shapes: v.shapes,
		})
		ps.targets[m] = v.t
		v.quiet = ps.p.Reserve(m).Warm.Floor.Covers(v.load)
		_, v.protected = ps.uncoveredLocations(v)
		views = append(views, v)
	}
	return views
}

// hibernationShapes are the usable shapes of hibernation-capable types the
// market holds or can buy as reserves.
func (ps *pass) hibernationShapes(m ReserveMarket) []FleetCapacity {
	if m.GPU != "" {
		return nil
	}
	var shapes []FleetCapacity
	for _, h := range ps.inMarket(m, func(h FleetHost) bool { return h.State != FleetFailed && h.State != FleetTerminating }) {
		if t, ok := ps.typeNamed(h.InstanceType); ok && t.Hibernates {
			shapes = append(shapes, t.Usable(ps.s.Offers.ReportedMemory[t.Name]))
		}
	}
	for _, o := range ps.marketOffers(m, true) {
		if o.Hibernate {
			shapes = append(shapes, o.Usable)
		}
	}
	return shapes
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
	return rootDiskMicros(h.Region, t.RootGiB(h.hibernating()))
}

func (ps *pass) cooled(h FleetHost) bool {
	return cooled(ps.s.Offers.Cooldowns, ps.s.Now, h.Region, h.InstanceType, h.Market)
}

func (ps *pass) room(m ReserveMarket) int { return ps.p.MaxGrowthActions - ps.used[m] }

func (ps *pass) act(a FleetAction) int {
	switch a.Kind {
	case ActionResume, ActionBuy, ActionBuyReserve, ActionRightsize, ActionRefresh:
		ps.used[a.Market]++
	case ActionReturnToReserve, ActionDrain, ActionRetireReserve, ActionConsolidate:
	}
	ps.plan.Actions = append(ps.plan.Actions, a)
	return len(ps.plan.Actions) - 1
}

func (ps *pass) buy(m ReserveMarket, o FleetOffer, reserve bool, containers []uuid.UUID) int {
	kind := ActionBuy
	var mode *ReserveMode
	if reserve {
		kind = ActionBuyReserve
		mode = ptr(ReserveStop)
		if o.Hibernate {
			mode = ptr(ReserveHibernate)
		}
		ps.reserveRoom--
	} else {
		ps.hostRoom--
	}
	ps.planned = append(ps.planned, plannedHost{market: m, offer: o, reserve: reserve})
	return ps.act(FleetAction{Kind: kind, Market: m, Offer: &o, Mode: mode, Containers: containers})
}

func (ps *pass) resume(m ReserveMarket, h *FleetHost, kind FleetActionKind, containers []uuid.UUID) int {
	h.State = FleetStarting
	ps.hostRoom--
	ps.reserveRoom++
	return ps.act(FleetAction{Kind: kind, Market: m, Host: ptr(h.ID), Containers: containers})
}

// pendingItem is one pending container during demand placement.
type pendingItem struct {
	id     uuid.UUID
	need   Requirement
	bought *HostID
	wait   int
}

// demand places pending containers: ready room, then the host bought for
// one, then starting hosts, then ready reserves resumed for them, then new
// hosts covering the rest.
func (ps *pass) demand() {
	var items []pendingItem
	for _, g := range ps.s.Pending {
		for _, c := range g.Containers {
			ps.plan.Waits = append(ps.plan.Waits, ContainerWait{Container: c.ID})
			items = append(items, pendingItem{id: c.ID, need: g.Need, bought: c.Host, wait: len(ps.plan.Waits) - 1})
		}
	}
	slices.SortStableFunc(items, func(a, b pendingItem) int { return size(b.need) - size(a.need) })
	var ready, coming []HostCapacity
	for _, h := range ps.hosts {
		if h.State == FleetServing {
			ready = append(ready, h.capacity())
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
		if fitFirst(ready, it.need) >= 0 {
			continue
		}
		ps.waiting[ps.demandMarket(it.need)] = true
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
// it is charged to.
func (ps *pass) demandMarket(need Requirement) ReserveMarket {
	if need.GPUsNeeded() == 0 {
		return ReserveMarket{Preemptible: need.Preemptible}
	}
	return ReserveMarket{GPU: ps.chargedModel(need)}
}

// chargedModel is the GPU model work is charged to: the best-ranked model
// it accepts; among equally ranked ones (as with "any"), a model the fleet
// already holds, then the cheapest per card.
func (ps *pass) chargedModel(need Requirement) string {
	type model struct {
		name    string
		rank    int
		stocked bool
		perCard int64
	}
	var models []model
	for _, t := range ps.s.Offers.Catalog {
		if t.GPU == "" || !GPUAccepted(need.GPUs, t.GPU) {
			continue
		}
		price := int64(-1)
		for _, region := range regionOrder() {
			if p, ok := t.OnDemandMicros(region); ok && (price < 0 || p < price) {
				price = p
			}
		}
		per := price / int64(t.GPUCount)
		if i := slices.IndexFunc(models, func(m model) bool { return m.name == t.GPU }); i >= 0 {
			models[i].perCard = min(models[i].perCard, per)
			continue
		}
		stocked := slices.ContainsFunc(ps.hosts, func(h FleetHost) bool {
			return h.GPU == t.GPU && h.State != FleetFailed && h.State != FleetTerminating
		})
		models = append(models, model{name: t.GPU, rank: GPURank(need.GPUs, t.GPU), stocked: stocked, perCard: per})
	}
	if len(models) == 0 {
		return GPUAny
	}
	best := slices.MinFunc(models, func(a, b model) int {
		return cmp.Or(cmp.Compare(a.rank, b.rank), boolOrder(!a.stocked, !b.stocked), cmp.Compare(a.perCard, b.perCard), strings.Compare(a.name, b.name))
	})
	return best.name
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
		m := ps.demandMarket(it.need)
		h := ps.reserveFor(it.need, m)
		if h == nil || ps.hostRoom <= 0 || ps.room(m) <= 0 {
			left = append(left, it)
			continue
		}
		capacity := h.capacity()
		capacity.Reserve(it.need)
		opened = append(opened, resumed{action: ps.resume(m, h, ActionResume, []uuid.UUID{it.id}), host: capacity})
		ps.provisioning(it, &opened[len(opened)-1].host.Host, nil)
	}
	return left
}

func (ps *pass) reserveFor(need Requirement, m ReserveMarket) *FleetHost {
	var candidates []*FleetHost
	for i := range ps.hosts {
		h := &ps.hosts[i]
		if !h.resumable() || !h.Current || h.Protected || ps.cooled(*h) || !h.capacity().Fits(need) {
			continue
		}
		if need.Preemptible && h.Market == MarketOnDemand && !ps.lendable(*h) {
			continue
		}
		candidates = append(candidates, h)
	}
	if len(candidates) == 0 {
		return nil
	}
	return slices.MinFunc(candidates, func(a, b *FleetHost) int {
		return cmp.Or(boolOrder(a.market() != m, b.market() != m), boolOrder(a.HourlyMicros == nil, b.HourlyMicros == nil),
			cmp.Compare(a.hourly(), b.hourly()), strings.Compare(a.ID.String(), b.ID.String()))
	})
}

// lendable reports whether Spot work may resume on-demand reserve h: the
// ready on-demand reserves left without it still meet their stopped target.
func (ps *pass) lendable(h FleetHost) bool {
	m := h.market()
	var held FleetCapacity
	for _, r := range ps.inMarket(m, func(r FleetHost) bool { return r.resumable() && r.Current && !r.Protected }) {
		held = held.Plus(r.Usable)
	}
	return held.Minus(h.Usable).Covers(ps.targets[m].Stopped)
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
		key := Requirement{Region: it.need.Region, Zone: it.need.Zone, Preemptible: it.need.Preemptible, GPUs: it.need.GPUs}
		n := slices.IndexFunc(classes, func(c *demandClass) bool {
			return c.need.Region == key.Region && c.need.Zone == key.Zone && c.need.Preemptible == key.Preemptible && slices.Equal(c.need.GPUs, key.GPUs)
		})
		if n < 0 {
			classes = append(classes, &demandClass{need: key, market: ps.demandMarket(it.need)})
			n = len(classes) - 1
		}
		c := classes[n]
		shape := FleetCapacity{CPUMillis: it.need.CPUMillis, MemoryBytes: it.need.MemoryBytes, GPUs: it.need.GPUsNeeded()}
		i := slices.Index(c.shapes, shape)
		if i < 0 {
			c.shapes = append(c.shapes, shape)
			c.containers = append(c.containers, nil)
			i = len(c.shapes) - 1
		}
		c.containers[i] = append(c.containers[i], it)
	}
	for _, c := range classes {
		offers := RankOffers(ps.p, c.need, false, ps.s.Offers)
		if c.market.GPU != "" {
			offers = slices.DeleteFunc(offers, func(o FleetOffer) bool { return o.Type.GPU != c.market.GPU })
		}
		need := CoverNeed{Items: make([]CoverItem, len(c.shapes))}
		for i, shape := range c.shapes {
			need.Items[i] = CoverItem{Shape: shape, Count: len(c.containers[i])}
		}
		result := Cover(offers, need, servingCost(ps.p), min(ps.room(c.market), ps.hostRoom))
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
			action := ps.buy(c.market, node.Offer, false, ids)
			for _, it := range taken {
				ps.provisioning(it, nil, ptr(action))
			}
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

// light records when each serving host without pinned work became lightly
// used.
func (ps *pass) light() {
	for _, h := range ps.s.Hosts {
		if h.State != FleetServing || h.Pinned > 0 || !lightlyUsed(h.Load, h.Usable, ps.p.ConsolidationPercent) {
			continue
		}
		since := ps.s.Now
		if h.LightSince != nil {
			since = *h.LightSince
		}
		ps.plan.LightSince[h.ID] = since
	}
}

func (ps *pass) lightFor(h FleetHost) time.Duration {
	since, ok := ps.plan.LightSince[h.ID]
	if !ok {
		return 0
	}
	return ps.s.Now.Sub(since)
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

// uncoveredLocations fits location demand onto serving and starting hosts
// and this pass's purchases, most specific first. It returns what is left
// and the serving hosts that took some.
func (ps *pass) uncoveredLocations(v *marketView) ([]LocationDemand, map[HostID]bool) {
	type room struct {
		host                 *HostID
		region, zone, zoneID string
		free                 FleetCapacity
	}
	var rooms []room
	for _, h := range ps.inMarket(v.m, func(h FleetHost) bool { return serving(h) || starting(h) }) {
		r := room{region: h.Region, zone: h.Zone, zoneID: h.ZoneID, free: h.free()}
		if h.State == FleetServing {
			r.host = ptr(h.ID)
		}
		rooms = append(rooms, r)
	}
	for _, n := range ps.planned {
		if n.market == v.m && !n.reserve {
			rooms = append(rooms, room{region: n.offer.Region, zone: n.offer.Zone, zoneID: n.offer.ZoneID, free: n.offer.Usable})
		}
	}
	demands := slices.Clone(v.locations)
	specific := func(d LocationDemand) int { return boolInt(d.Region != "") + boolInt(d.Zone != "") }
	slices.SortStableFunc(demands, func(a, b LocationDemand) int {
		return cmp.Or(cmp.Compare(specific(b), specific(a)), cmp.Compare(b.Shape.GPUs, a.Shape.GPUs), cmp.Compare(b.Shape.MemoryBytes, a.Shape.MemoryBytes))
	})
	protected := map[HostID]bool{}
	var unmet []LocationDemand
	for _, d := range demands {
		left := d.Count
		for i := range rooms {
			if left == 0 {
				break
			}
			if !d.accepts(rooms[i].region, rooms[i].zone, rooms[i].zoneID) {
				continue
			}
			if n := rooms[i].free.fits(d.Shape, left); n > 0 {
				rooms[i].free = rooms[i].free.Minus(d.Shape.Times(n))
				left -= n
				if rooms[i].host != nil {
					protected[*rooms[i].host] = true
				}
			}
		}
		if left > 0 {
			d.Count = left
			unmet = append(unmet, d)
		}
	}
	return unmet, protected
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// grow keeps the warm target and recent shapes ready: resume reserves,
// then buy for location demand, then buy the cheapest cover of what is
// short.
func (ps *pass) grow(v *marketView) {
	if !v.mayGrow {
		return
	}
	short := v.t.Warm.Minus(ps.warmFree(v)).Clamp().Minus(ps.warmPending(v)).Clamp()
	shapes := ps.uncoveredShapes(v)
	reserves := ps.inMarket(v.m, func(h FleetHost) bool { return h.resumable() && h.Current && !h.Protected && !ps.cooled(h) })
	locations, _ := ps.uncoveredLocations(v)
	serves := func(h *FleetHost, demands []LocationDemand) bool {
		return slices.ContainsFunc(demands, func(d LocationDemand) bool { return d.accepts(h.Region, h.Zone, h.ZoneID) && h.Usable.Covers(d.Shape) })
	}
	slices.SortStableFunc(reserves, func(a, b *FleetHost) int {
		return cmp.Or(boolOrder(!serves(a, locations), !serves(b, locations)), boolOrder(a.HourlyMicros == nil, b.HourlyMicros == nil),
			cmp.Compare(a.hourly(), b.hourly()), strings.Compare(a.ID.String(), b.ID.String()))
	})
	for _, h := range reserves {
		if ps.room(v.m) <= 0 || ps.hostRoom <= 0 {
			break
		}
		unmet, _ := ps.uncoveredLocations(v)
		if len(unmet) > 0 && !serves(h, unmet) {
			continue
		}
		if len(unmet) == 0 && short.Empty() && !slices.ContainsFunc(shapes, h.Usable.Covers) {
			continue
		}
		ps.resume(v.m, h, ActionResume, nil)
		short = short.Minus(h.Usable).Clamp()
		shapes = slices.DeleteFunc(shapes, h.Usable.Covers)
	}
	locations, _ = ps.uncoveredLocations(v)
	for _, d := range locations {
		offers := slices.DeleteFunc(slices.Clone(ps.marketOffers(v.m, false)), func(o FleetOffer) bool { return !d.accepts(o.Region, o.Zone, o.ZoneID) })
		result := Cover(offers, CoverNeed{Items: []CoverItem{{Shape: d.Shape, Count: d.Count}}}, servingCost(ps.p), min(ps.room(v.m), ps.hostRoom))
		for _, node := range result.Nodes {
			ps.buy(v.m, node.Offer, false, nil)
			short = short.Minus(node.Offer.Usable).Clamp()
			shapes = slices.DeleteFunc(shapes, node.Offer.Usable.Covers)
		}
	}
	if !short.Empty() || len(shapes) > 0 {
		result := Cover(ps.marketOffers(v.m, false), CoverNeed{Aggregate: short, Shapes: shapes}, servingCost(ps.p), min(ps.room(v.m), ps.hostRoom))
		for _, node := range result.Nodes {
			ps.buy(v.m, node.Offer, false, nil)
		}
		short = short.Minus(result.Supplied).Clamp()
		shapes = result.UnmetShapes
	}
	v.shortfall, v.unmet = short, shapes
}

// uncoveredShapes are recent shapes no serving host's free room, starting
// host or purchase in this pass fits.
func (ps *pass) uncoveredShapes(v *marketView) []FleetCapacity {
	return slices.DeleteFunc(slices.Clone(v.shapes), func(shape FleetCapacity) bool {
		return slices.ContainsFunc(ps.inMarket(v.m, serving), func(h *FleetHost) bool { return h.free().Covers(shape) }) ||
			slices.ContainsFunc(ps.inMarket(v.m, starting), func(h *FleetHost) bool { return h.Usable.Covers(shape) }) ||
			slices.ContainsFunc(ps.planned, func(n plannedHost) bool { return n.market == v.m && !n.reserve && n.offer.Usable.Covers(shape) })
	})
}

// consolidate picks the lightly used host whose interruptible work moves
// onto another host of its placement: most expensive first, then least
// loaded. It moves once the host has been light for ConsolidationLight,
// one per market, outside any shortfall or cooldown.
func (ps *pass) consolidate(v *marketView) {
	hosts := ps.inMarket(v.m, serving)
	warmFree := ps.warmFree(v)
	if len(hosts) < 2 || !v.t.Warm.Minus(warmFree).Clamp().Empty() || ps.consolidating(v.m) {
		return
	}
	available := func(h *FleetHost) bool { return !h.Protected && !v.protected[h.ID] }
	destinations := func(c *FleetHost) []*FleetHost {
		var out []*FleetHost
		for _, d := range hosts {
			if d.ID != c.ID && available(d) && d.Region == c.Region && d.Zone == c.Zone && d.free().Covers(c.Load) {
				out = append(out, d)
			}
		}
		return out
	}
	var candidates []*FleetHost
	for _, h := range hosts {
		if h.Containers == 0 || h.Pinned > 0 || !lightlyUsed(h.Load, h.Usable, ps.p.ConsolidationPercent) || !available(h) ||
			!h.settled(ps.p, ps.s.Now) || !warmFree.Minus(h.Usable).Covers(v.t.Warm) || len(destinations(h)) == 0 {
			continue
		}
		fits := !slices.ContainsFunc(v.shapes, func(shape FleetCapacity) bool {
			return !slices.ContainsFunc(hosts, func(o *FleetHost) bool { return o.ID != h.ID && o.free().Minus(h.Load).Covers(shape) })
		})
		if fits {
			candidates = append(candidates, h)
		}
	}
	if len(candidates) == 0 {
		return
	}
	c := slices.MinFunc(candidates, func(a, b *FleetHost) int {
		return cmp.Or(cmp.Compare(b.hourly(), a.hourly()), cmp.Compare(a.Load.CPUMillis, b.Load.CPUMillis),
			cmp.Compare(a.Load.MemoryBytes, b.Load.MemoryBytes), strings.Compare(a.ID.String(), b.ID.String()))
	})
	v.candidate = ptr(c.ID)
	if ps.lightFor(*c) < ps.p.ConsolidationLight {
		return
	}
	d := slices.MinFunc(destinations(c), func(a, b *FleetHost) int {
		return cmp.Or(cmp.Compare(a.free().Minus(c.Load).MemoryBytes, b.free().Minus(c.Load).MemoryBytes), strings.Compare(a.ID.String(), b.ID.String()))
	})
	v.moving, v.protected[d.ID] = ptr(c.ID), true
	ps.act(FleetAction{Kind: ActionConsolidate, Market: v.m, Host: ptr(c.ID), Destination: ptr(d.ID)})
}

// consolidating reports a consolidation in progress or cooling down.
func (ps *pass) consolidating(m ReserveMarket) bool {
	r, ok := ps.s.Markets[m]
	if !ok {
		return false
	}
	if r.CooldownUntil != nil && r.CooldownUntil.After(ps.s.Now) {
		return true
	}
	return r.ConsolidatingHost != nil && r.ConsolidationStarted != nil && ps.s.Now.Sub(*r.ConsolidationStarted) < ps.p.ConsolidationDeadline
}

// retain releases idle hosts the market no longer needs: most expensive
// first, then largest first when quiet and smallest first when loaded. A
// host leaves only while the surplus over the warm target covers it and
// every recent shape that fits now still fits another host.
func (ps *pass) retain(v *marketView) {
	hosts := ps.inMarket(v.m, func(h FleetHost) bool { return serving(h) && (v.moving == nil || h.ID != *v.moving) })
	warmFree := ps.warmFree(v)
	if !v.t.Warm.Minus(warmFree).Clamp().Empty() {
		return
	}
	surplus := warmFree.Minus(v.t.Warm)
	if v.moving != nil {
		for _, h := range ps.inMarket(v.m, serving) {
			if h.ID == *v.moving {
				surplus = surplus.Minus(h.Usable)
			}
		}
	}
	wait := max(ps.p.IdleTimeout, ps.p.ConsolidationLight)
	var idle []*FleetHost
	for _, h := range hosts {
		if h.Containers == 0 && !h.Protected && !v.protected[h.ID] && h.settled(ps.p, ps.s.Now) && ps.lightFor(*h) >= wait {
			idle = append(idle, h)
		}
	}
	// Largest first when quiet, smallest first when loaded.
	extent := func(h *FleetHost) FleetCapacity {
		if v.quiet {
			return FleetCapacity{}.Minus(h.Usable)
		}
		return h.Usable
	}
	slices.SortStableFunc(idle, func(a, b *FleetHost) int {
		return cmp.Or(cmp.Compare(b.hourly(), a.hourly()), cmp.Compare(extent(a).CPUMillis, extent(b).CPUMillis),
			cmp.Compare(extent(a).MemoryBytes, extent(b).MemoryBytes), strings.Compare(a.ID.String(), b.ID.String()))
	})
	feasible := slices.DeleteFunc(slices.Clone(v.shapes), func(shape FleetCapacity) bool {
		return !slices.ContainsFunc(hosts, func(h *FleetHost) bool { return h.free().Covers(shape) })
	})
	remaining := slices.Clone(hosts)
	for _, h := range idle {
		if !surplus.Minus(h.Usable).Covers(FleetCapacity{}) {
			continue
		}
		others := slices.DeleteFunc(slices.Clone(remaining), func(o *FleetHost) bool { return o.ID == h.ID })
		if slices.ContainsFunc(feasible, func(shape FleetCapacity) bool {
			return !slices.ContainsFunc(others, func(o *FleetHost) bool { return o.free().Covers(shape) })
		}) {
			continue
		}
		surplus, remaining = surplus.Minus(h.Usable), others
		ps.leave(v, h)
	}
}

// leave returns a leaving host to the reserve while the market's reserve
// without it falls short, and drains it otherwise.
func (ps *pass) leave(v *marketView, h *FleetHost) {
	_, catalogued := ps.typeNamed(h.InstanceType)
	if ps.reserveRoom > 0 && catalogued && !v.t.Stopped.Empty() && !ps.reserveHeld(v).Covers(v.t.Stopped) {
		mode := ReserveStop
		if h.HibernationConfigured && v.m.GPU == "" {
			mode = ReserveHibernate
		}
		h.State, h.ReserveMode, h.Current = FleetPreparing, ptr(mode), false
		ps.reserveRoom--
		ps.hostRoom++
		ps.act(FleetAction{Kind: ActionReturnToReserve, Market: v.m, Host: ptr(h.ID), Mode: ptr(mode)})
		return
	}
	h.State = FleetDraining
	ps.act(FleetAction{Kind: ActionDrain, Market: v.m, Host: ptr(h.ID)})
}

// reserveHeld is the market's reserve capacity, ready or not, with this
// pass's reserve purchases.
func (ps *pass) reserveHeld(v *marketView) FleetCapacity {
	held := totalOf(ps.inMarket(v.m, func(h FleetHost) bool { return h.reserve() && !v.retired[h.ID] }), func(h *FleetHost) FleetCapacity { return h.Usable })
	for _, n := range ps.planned {
		if n.market == v.m && n.reserve {
			held = held.Plus(n.offer.Usable)
		}
	}
	return held
}

// reserves fills the hibernation and stopped targets by buying reserves,
// or retires reserves the targets no longer need.
func (ps *pass) reserves(v *marketView) {
	committed := totalOf(ps.inMarket(v.m, FleetHost.hibernatingReserve), func(h *FleetHost) FleetCapacity { return h.Usable })
	v.hibernate = v.t.Hibernation.Minus(committed).Clamp()
	limit := func() int { return min(ps.room(v.m), ps.reserveRoom) }
	if v.mayGrow && !v.hibernate.Empty() {
		offers := slices.DeleteFunc(slices.Clone(ps.marketOffers(v.m, true)), func(o FleetOffer) bool { return !o.Hibernate })
		result := Cover(offers, CoverNeed{Aggregate: v.hibernate}, reserveCost(ps.p), limit())
		for _, node := range result.Nodes {
			ps.buy(v.m, node.Offer, true, nil)
		}
		v.hibernate = v.hibernate.Minus(result.Supplied).Clamp()
	}
	v.stopped = v.t.Stopped.Minus(ps.reserveHeld(v)).Clamp()
	if !v.t.Stopped.Empty() {
		v.stoppedOut = slices.DeleteFunc(slices.Clone(v.shapes), func(shape FleetCapacity) bool {
			return slices.ContainsFunc(ps.inMarket(v.m, FleetHost.reserve), func(h *FleetHost) bool { return h.Usable.Covers(shape) }) ||
				slices.ContainsFunc(ps.planned, func(n plannedHost) bool { return n.market == v.m && n.reserve && n.offer.Usable.Covers(shape) })
		})
	}
	if v.stopped.Empty() && len(v.stoppedOut) == 0 {
		ps.retire(v)
		return
	}
	if !v.mayGrow {
		return
	}
	result := Cover(ps.marketOffers(v.m, true), CoverNeed{Aggregate: v.stopped, Shapes: v.stoppedOut}, reserveCost(ps.p), limit())
	for _, node := range result.Nodes {
		ps.buy(v.m, node.Offer, true, nil)
	}
	v.stopped, v.stoppedOut = v.stopped.Minus(result.Supplied).Clamp(), result.UnmetShapes
}

// retire terminates reserves beyond the stopped target, non-growable and
// most expensive first. It keeps the ready, hibernated and committed
// hibernation capacity the targets need and the only ready reserve that
// fits a recent shape; a pending reserve cannot stand in for a ready one.
func (ps *pass) retire(v *marketView) {
	reserves := ps.inMarket(v.m, FleetHost.reserve)
	ready := func(h *FleetHost) bool { return h.resumable() && h.Current }
	saved := func(h *FleetHost) bool { return h.State == FleetImageSaved && h.Current }
	sum := func(keep func(*FleetHost) bool) FleetCapacity {
		return totalOf(reserves, func(h *FleetHost) FleetCapacity {
			if keep(h) && !v.retired[h.ID] {
				return h.Usable
			}
			return FleetCapacity{}
		})
	}
	held := sum(func(*FleetHost) bool { return true })
	usable, fast, committed := sum(ready), sum(saved), sum(func(h *FleetHost) bool { return h.hibernatingReserve() })
	hibernation := v.t.Hibernation.Upper(fast.Lower(v.t.Stopped))
	needUsable, needFast, needCommitted := usable.Lower(v.t.Stopped), fast.Lower(hibernation), committed.Lower(hibernation)
	candidates := slices.DeleteFunc(slices.Clone(reserves), func(h *FleetHost) bool { return h.Protected || h.State == FleetStopping })
	growable := func(h *FleetHost) bool {
		_, ok := ps.typeNamed(h.InstanceType)
		return ok && !ps.cooled(*h)
	}
	slices.SortStableFunc(candidates, func(a, b *FleetHost) int {
		return cmp.Or(boolOrder(growable(a), growable(b)), cmp.Compare(ps.stoppedMicros(*b), ps.stoppedMicros(*a)),
			cmp.Compare(b.Usable.CPUMillis, a.Usable.CPUMillis), cmp.Compare(b.Usable.MemoryBytes, a.Usable.MemoryBytes),
			strings.Compare(a.ID.String(), b.ID.String()))
	})
	for _, h := range candidates {
		if !held.Minus(h.Usable).Covers(v.t.Stopped) ||
			(h.hibernatingReserve() && !committed.Minus(h.Usable).Covers(needCommitted)) ||
			(saved(h) && !fast.Minus(h.Usable).Covers(needFast)) ||
			(ready(h) && !usable.Minus(h.Usable).Covers(needUsable)) {
			continue
		}
		if !v.t.Stopped.Empty() && slices.ContainsFunc(v.shapes, func(shape FleetCapacity) bool {
			return h.Usable.Covers(shape) && !slices.ContainsFunc(reserves, func(o *FleetHost) bool {
				return o.ID != h.ID && ready(o) && !v.retired[o.ID] && o.Usable.Covers(shape)
			})
		}) {
			continue
		}
		v.retired[h.ID] = true
		held = held.Minus(h.Usable)
		if ready(h) {
			usable = usable.Minus(h.Usable)
		}
		if saved(h) {
			fast = fast.Minus(h.Usable)
		}
		if h.hibernatingReserve() {
			committed = committed.Minus(h.Usable)
		}
		ps.reserveRoom++
		ps.act(FleetAction{Kind: ActionRetireReserve, Market: v.m, Host: ptr(h.ID)})
	}
}

// hibernatingReserve is a reserve asked to hibernate. A refused hibernation
// that fell back to a plain stop keeps its slot until demand uses it.
func (h FleetHost) hibernatingReserve() bool { return h.reserve() && h.hibernating() }

// rightsize replaces the idle host whose replacement pays back most: the
// hourly saving over the cost horizon must exceed the new host's cost
// while it provisions. Nothing else may be growing or moving in the market.
func (ps *pass) rightsize(v *marketView) {
	growing := slices.ContainsFunc(ps.plan.Actions, func(a FleetAction) bool {
		return a.Market == v.m && (a.Kind == ActionResume || a.Kind == ActionBuy || a.Kind == ActionBuyReserve)
	})
	if !v.mayGrow || growing || !ps.warmPending(v).Empty() || v.moving != nil || ps.consolidating(v.m) || ps.room(v.m) <= 0 || ps.hostRoom <= 0 {
		return
	}
	hosts := ps.inMarket(v.m, serving)
	type choice struct {
		payback int64
		host    *FleetHost
		offer   FleetOffer
	}
	var best *choice
	horizon, provision := int64(ps.p.CostHorizon/time.Second), int64(ps.p.Provision/time.Second)
	for _, h := range hosts {
		if h.Containers > 0 || h.Protected || v.protected[h.ID] || !h.settled(ps.p, ps.s.Now) || h.HourlyMicros == nil || ps.lightFor(*h) < ps.p.ConsolidationLight {
			continue
		}
		others := slices.DeleteFunc(slices.Clone(hosts), func(o *FleetHost) bool { return o.ID == h.ID })
		need := v.t.Warm.Minus(totalOf(others, func(o *FleetHost) FleetCapacity { return o.free() })).Clamp()
		if need.Empty() {
			continue
		}
		for _, o := range ps.marketOffers(v.m, false) {
			if o.Region != h.Region || o.Zone != h.Zone || !o.Usable.Covers(need) {
				continue
			}
			if slices.ContainsFunc(v.shapes, func(shape FleetCapacity) bool {
				return !o.Usable.Covers(shape) && !slices.ContainsFunc(others, func(x *FleetHost) bool { return x.free().Covers(shape) })
			}) {
				continue
			}
			saving := *h.HourlyMicros - o.HourlyMicros
			payback := saving*horizon - o.HourlyMicros*provision
			if saving > 0 && payback > 0 && (best == nil || payback > best.payback) {
				best = &choice{payback: payback, host: h, offer: o}
			}
		}
	}
	if best == nil {
		return
	}
	ps.hostRoom--
	ps.planned = append(ps.planned, plannedHost{market: v.m, offer: best.offer})
	ps.act(FleetAction{Kind: ActionRightsize, Market: v.m, Host: ptr(best.host.ID), Offer: &best.offer})
}

// refresh resumes reserves prepared for an older agent release so they
// update in place and stop again.
func (ps *pass) refresh(v *marketView) {
	for _, h := range ps.inMarket(v.m, func(h FleetHost) bool { return h.resumable() && !h.Current && !h.Protected && !ps.cooled(h) }) {
		if v.retired[h.ID] || ps.room(v.m) <= 0 || ps.hostRoom <= 0 {
			continue
		}
		ps.hostRoom--
		ps.act(FleetAction{Kind: ActionRefresh, Market: v.m, Host: ptr(h.ID)})
	}
}

// report is the market's published plan.
func (ps *pass) report(v *marketView) MarketPlan {
	var reserve, ready, saved, unverified FleetCapacity
	for _, h := range ps.inMarket(v.m, FleetHost.reserve) {
		if v.retired[h.ID] {
			continue
		}
		reserve = reserve.Plus(h.Usable)
		if h.resumable() && h.Current {
			ready = ready.Plus(h.Usable)
			if h.State == FleetImageSaved {
				saved = saved.Plus(h.Usable)
			}
			if h.State == FleetHibernateUnverified {
				unverified = unverified.Plus(h.Usable)
			}
		}
	}
	unmetLocations, _ := ps.uncoveredLocations(v)
	plan := MarketPlan{
		Market: v.m, Load: v.load, Quiet: v.quiet,
		WarmTarget: v.t.Warm, WarmFree: ps.warmFree(v), WarmPending: ps.warmPending(v),
		StoppedTarget: v.t.Stopped, ReserveCapacity: reserve, ReserveReady: ready, ReservePending: reserve.Minus(ready).Clamp(),
		HibernationTarget: v.t.Hibernation, Hibernated: saved, HibernationUnverified: unverified,
		Shortfall: v.shortfall, StoppedShortfall: v.stopped, HibernationShortfall: v.hibernate,
		UnmetShapes: v.unmet, UnmetStoppedShapes: v.stoppedOut, UnmetLocations: unmetLocations,
		States: ps.states(v.m), Forecast: v.forecast, ConsolidationCandidate: v.candidate, Consolidates: v.moving,
	}
	short := !plan.Shortfall.Empty() || !plan.StoppedShortfall.Empty() || !plan.HibernationShortfall.Empty() ||
		len(plan.UnmetShapes) > 0 || len(plan.UnmetStoppedShapes) > 0 || len(unmetLocations) > 0
	switch {
	case !v.mayGrow:
		plan.Reason = ReasonDemandOrRecovery
	case !short:
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

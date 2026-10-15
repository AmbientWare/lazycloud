package compute

import (
	"cmp"
	"slices"
	"strings"
	"time"
)

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

// retain releases the idle hosts the market no longer needs once each has
// been idle for its market's timeout.
func (ps *pass) retain(v *marketView) {
	timeout := ps.p.IdleTimeout
	if v.m.spotCPU() {
		timeout = ps.p.SpotIdleTimeout
	}
	for _, h := range ps.leavers(v) {
		if ps.s.Now.Sub(ps.plan.IdleSince[h.ID]) >= timeout {
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

// returnable reports whether host h of market v may stop into the reserve:
// EC2 can stop it, its type is known, it is no interruption's, and it lies
// where the market's starts read.
func (ps *pass) returnable(v *marketView, h FleetHost) bool {
	_, catalogued := ps.typeNamed(h.InstanceType)
	return catalogued && h.Stoppable && !h.Protected && !ps.misplaced(v, h.Region)
}

// leave returns a leaving host to the reserve when it is returnable and the
// market's placed reserves fall short, hold nothing
// that fits the largest shape while the host does, or the host slept there
// before and they hold less than its share of the peak, so a reserve woken
// for a burst waits stopped for the next. A host launched able to
// hibernate does where hibernates allows. Any other leaving host drains.
func (ps *pass) leave(v *marketView, h *FleetHost) {
	placed := ps.placed(v)
	var short *ActionReason
	switch {
	case !ps.returnable(v, *h):
	case !ps.floor(v, HostID{}, placed).Covers(v.stopped):
		short = ptr(ReasonReserve)
	case ps.fitsLargest(v, *h) && !ps.holdsLargest(v, HostID{}):
		short = ptr(ReasonLargest)
	case h.Slept && !ps.floor(v, HostID{}, placed).Covers(v.peakShare):
		short = ptr(ReasonPeak)
	}
	if ps.reserveRoom > 0 && short != nil {
		mode := ReserveStop
		if t, _ := ps.typeNamed(h.InstanceType); h.HibernationConfigured && hibernates(t) {
			mode = ReserveHibernate
		}
		h.State, h.ReserveMode, h.Current = FleetPreparing, ptr(mode), false
		// It runs until it stops, so it keeps its host room.
		ps.reserveRoom--
		ps.act(FleetAction{Kind: ActionReturnToReserve, Reason: *short, Market: v.m, Host: ptr(h.ID), Mode: ptr(mode)})
		return
	}
	h.State = FleetDraining
	ps.act(FleetAction{Kind: ActionDrain, Reason: ReasonIdle, Market: v.m, Host: ptr(h.ID)})
}

// floor is what holds the market's stopped target: the reserves keep
// selects, less skip, retired and interrupted ones, which never resume.
func (ps *pass) floor(v *marketView, skip HostID, keep func(FleetHost) bool) FleetCapacity {
	return totalOf(ps.inMarket(v.m, func(h FleetHost) bool {
		return h.reserve() && !h.Protected && keep(h) && h.ID != skip && !v.retired[h.ID]
	}), func(h *FleetHost) FleetCapacity { return h.Usable })
}

// holdsLargest reports whether a placed reserve of the market other than
// skip, or one this pass buys, fits the market's largest shape and its
// disks.
func (ps *pass) holdsLargest(v *marketView, skip HostID) bool {
	if v.largest.Empty() {
		return true
	}
	for _, h := range ps.inMarket(v.m, ps.placed(v)) {
		if h.ID != skip && !h.Protected && !v.retired[h.ID] && ps.fitsLargest(v, *h) {
			return true
		}
	}
	return slices.ContainsFunc(ps.planned, func(n plannedHost) bool {
		return n.market == v.m && n.reserve && v.fits(n.offer.Usable, n.offer.Type) && !ps.misplaced(v, n.offer.Region)
	})
}

// returning reports whether market v waits for hosts to return to the
// reserve before it buys what the reserve lacks, short in total and, with
// lacksLargest, a host that fits the largest shape: while any host could
// return and the reserve has lacked it for less than the return wait or
// the market awaits returns, or while hosts due back soon cover it. A host
// could return when it serves or starts and is returnable. It is due back
// soon when it starts, became ready within the
// return wait, as a reserve resumed for a burst does, or idles holding no
// warm slot, slots a cheaper type will take over, or only a build's slot
// with room for the largest shape, which it holds warm until the slot
// lapses.
func (ps *pass) returning(v *marketView, short FleetCapacity, lacksLargest bool) bool {
	leaving := ps.leavers(v)
	could := ps.inMarket(v.m, func(h FleetHost) bool { return (serving(h) || starting(h)) && ps.returnable(v, h) })
	if len(could) > 0 && (ps.p.Reserve(v.m).AwaitReturn || ps.s.Now.Sub(*v.floorShortSince) < ps.p.ReturnWait) {
		return true
	}
	soon := slices.DeleteFunc(could, func(h *FleetHost) bool {
		replaced, _ := ps.replacement(v, *h)
		_, idle := ps.plan.IdleSince[h.ID]
		holdsLargest := idle && !ps.holds(h.ID, slotFloor, slotLoad) && !v.largest.Empty() && ps.fitsLargest(v, *h)
		return !starting(*h) && ps.s.Now.Sub(h.PhaseAt) >= ps.p.ReturnWait && replaced == nil && !holdsLargest &&
			!slices.ContainsFunc(leaving, func(l *FleetHost) bool { return l.ID == h.ID })
	})
	return totalOf(soon, func(h *FleetHost) FleetCapacity { return h.Usable }).Covers(short) &&
		(!lacksLargest || slices.ContainsFunc(soon, func(h *FleetHost) bool { return ps.fitsLargest(v, *h) }))
}

// reserves keeps market v's stopped target, among it a reserve that fits
// the largest shape. What they lack is bought once no work waits in the
// market, the market no longer waits for hosts to return and containers
// have stopped arriving. With the target held, the reserves the market no
// longer needs retire.
func (ps *pass) reserves(v *marketView) {
	ps.retireUnresumable(v)
	short := v.stopped.Minus(ps.floor(v, HostID{}, ps.placed(v))).Clamp()
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
		v.stoppedReason = WaitDemand
	case ps.returning(v, short, !item.Empty()):
		v.stoppedReason = WaitReturning
	case ps.batching():
		v.stoppedReason = WaitBatch
	case v.loaded:
		// A share of load sets the target: the cheapest cover holds it, and
		// whichever of its hosts fits the largest shape holds that. In the
		// CPU Spot market each reserve holds the target up to the default
		// largest shape, the size that still hibernates, so a target growing
		// a little each pass buys a few large reserves that resume from
		// memory and cost about what small ones do to keep stopped.
		need := CoverNeed{Aggregate: short}
		if !item.Empty() {
			need = CoverNeed{Items: []CoverItem{{Shape: item, Disks: v.largestDisks, Count: 1}}, Aggregate: short.Minus(item).Clamp()}
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
		// where it can; the rest of the target beyond that reserve takes the
		// cheapest room, so smaller hosts never stand in for it.
		var supplied FleetCapacity
		unmet := false
		if !item.Empty() {
			result := ps.buyReserves(v, CoverNeed{Items: []CoverItem{{Shape: item, Disks: v.largestDisks, Count: 1}}}, item, FleetCapacity{})
			for _, node := range result.Nodes {
				supplied = supplied.Plus(node.Offer.Usable)
			}
			unmet = len(result.UnmetItems) > 0
		}
		if rest := short.Minus(supplied.Upper(item)).Clamp(); !rest.Empty() {
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

// retireUnresumable retires the market's stopped reserves that stopped
// after their reclaim notice: they never resume, so they hold none of the
// target.
func (ps *pass) retireUnresumable(v *marketView) {
	for _, h := range ps.inMarket(v.m, func(h FleetHost) bool { return h.resumable() && h.Protected }) {
		v.retired[h.ID] = true
		ps.reserveRoom++
		ps.act(FleetAction{Kind: ActionRetireReserve, Reason: ReasonInterrupted, Market: v.m, Host: ptr(h.ID)})
	}
}

// reserveReason settles market v's reserve shortfall after its purchases:
// held, or named by why the purchases fell short of item.
func (ps *pass) reserveReason(v *marketView, item FleetCapacity) {
	if v.stoppedShort.Empty() {
		v.floorShortSince = nil
		return
	}
	v.stoppedReason = ps.shortReason(v.m, slotNeed(v.m, item), true)
}

// buyReserves buys the cheapest reserves that cover need, ones that
// hibernate wherever one fits shape and the largest shape's disks, each
// holding least where it can: what those cannot cover, under a quota
// or a cooldown, any offer covers.
func (ps *pass) buyReserves(v *marketView, need CoverNeed, shape, least FleetCapacity) CoverResult {
	offers := ps.marketOffers(v.m, true)
	if !shape.Empty() {
		offers = preferHibernating(offers, shape, v.largestDisks)
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
	result := Cover(offers, need, reserveCost(ps.p), ps.limits(min(ps.room(v.m), ps.reserveRoom, ps.hostRoom), ps.marketRanking(v.m, true)))
	for _, node := range result.Nodes {
		holds, reason := node.Offer.Usable.Lower(need.Aggregate), ReasonReserve
		for i, n := range node.Placed {
			if n > 0 {
				holds, reason = holds.Upper(need.Items[i].Shape), ReasonLargest
			}
		}
		ps.buy(v.m, node.Offer, ActionBuyReserve, reason, holds, nil, nil)
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
		if holding && growable(h) && !stale(*h) && (ready(*h) || h.State == FleetPreparing) {
			continue
		}
		if !ps.floor(v, h.ID, ps.placed(v)).Covers(v.stopped) || !ps.floor(v, h.ID, ready).Covers(needReady) ||
			!ps.holdsLargest(v, h.ID) {
			continue
		}
		v.retired[h.ID] = true
		ps.reserveRoom++
		ps.act(FleetAction{Kind: ActionRetireReserve, Reason: ReasonSurplus, Market: v.m, Host: ptr(h.ID)})
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
		ps.buy(v.m, *best, ActionRightsize, ReasonCheaper, holds, nil, ptr(replaced.ID))
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

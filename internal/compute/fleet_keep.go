package compute

import (
	"cmp"
	"slices"
	"strings"
	"time"
)

// keep walks market v's running hosts in one order and keeps a prefix of
// them: every host that stays, then idle hosts until the kept serving ones
// cover the running target and its headroom fits free on them in parts. Every
// host after the cut is surplus and leaves. The order is the same on every
// replica and every pass, so two idle hosts never each count the other and
// both leave. Only serving hosts cover: a host coming never justifies
// cutting one that serves. A host that is leaving covers only the load it
// runs: its free room never cuts a host that would stay, though it is room
// the market need not buy. A host EC2 has not launched yet is cut once
// every kept host and purchase covers what this pass's load and headroom
// ask for: cancelling it is free, so the held target never keeps it.
func (ps *pass) keep(v *marketView) {
	// ready is the free room of the kept serving hosts, rooms that of every
	// kept host and purchase.
	var ready, rooms []FleetCapacity
	for _, n := range ps.planned {
		if n.market == v.m && !n.reserve {
			v.capacity = v.capacity.Plus(n.offer.Usable)
			rooms = append(rooms, n.offer.Usable.Minus(n.holds).Clamp())
		}
	}
	for _, h := range ps.keepOrder(v) {
		surplus := v.covered.Covers(v.holds.Running.Load) && placesAll(ready, v.heldParts)
		if h.Requested {
			surplus = v.capacity.Covers(v.want) && placesAll(rooms, v.wantParts)
		}
		if !ps.stays(*h) && surplus {
			ps.leave(v, h)
			continue
		}
		v.kept = append(v.kept, h)
		// A host updating its agent holds its room while it updates.
		v.capacity, rooms = v.capacity.Plus(h.Usable), append(rooms, h.room())
		switch {
		case !serving(*h):
		case ps.leaving(v, *h):
			v.covered = v.covered.Plus(h.Load)
		default:
			v.covered, ready = v.covered.Plus(h.Usable), append(ready, h.room())
		}
	}
	v.unplaced = unplaced(rooms, v.wantParts)
}

// stays reports a host the keep order never cuts: one that runs work or
// has work pending for it, one starting or updating its agent, which cannot
// be cancelled, and a rightsize's replacement, which the host it replaces
// waits for.
func (ps *pass) stays(h FleetHost) bool {
	return busy(h) || ps.claimed[h.ID] || starting(h) && !h.Requested || h.UpdateDue || h.Requested && h.Replaces != nil
}

// leaving reports a kept host whose free room is no headroom: one a
// rightsize replaces, which leaves once its replacement serves, or one
// resumed from the reserve that runs work, which returns once the work
// ends. An idle resumed host is headroom until a rightsize replaces it.
func (ps *pass) leaving(v *marketView, h FleetHost) bool {
	return h.Slept && ps.stays(h) || len(ps.inMarket(v.m, func(r FleetHost) bool { return r.Replaces != nil && *r.Replaces == h.ID })) > 0
}

// keepOrder is market v's serving, starting and requested hosts, reserves
// aside, in the order keep walks them: hosts that stay first, then idle
// hosts cheapest to serve first, those resumed from the reserve after
// them so they can go back, those EC2 has not launched yet last so they
// are cancelled first, then by id.
func (ps *pass) keepOrder(v *marketView) []*FleetHost {
	out := ps.inMarket(v.m, func(h FleetHost) bool { return !h.reserve() && (serving(h) || starting(h) || h.Requested) })
	slices.SortStableFunc(out, func(a, b *FleetHost) int {
		return cmp.Or(boolOrder(!ps.stays(*a), !ps.stays(*b)), boolOrder(a.Requested, b.Requested), boolOrder(a.Slept, b.Slept), ps.retention(a, b))
	})
	return out
}

// returnable reports whether host h may stop into the reserve: EC2 can stop
// it, its type is known and it is no interruption's.
func (ps *pass) returnable(h FleetHost) bool {
	_, catalogued := ps.typeNamed(h.InstanceType)
	return catalogued && h.Stoppable && !h.Protected
}

// leave removes a surplus host: a requested one is cancelled; a serving one
// returns to the reserve when it is returnable and the market's reserves
// fall short of the stopped target or hold nothing that fits the largest
// shape while the host does, hibernating where it launched able to;
// otherwise it drains.
func (ps *pass) leave(v *marketView, h *FleetHost) {
	if h.Requested {
		ps.act(FleetAction{Kind: ActionCancel, Reason: ReasonUnclaimed, Market: v.m, Host: ptr(h.ID)})
		return
	}
	var short *ActionReason
	switch {
	case !ps.returnable(*h):
	case !ps.reserveSupply(v, h.ID).Covers(v.holds.Stopped.Load):
		short = ptr(ReasonReserve)
	case ps.fitsLargest(v, *h) && !ps.holdsLargest(v, h.ID):
		short = ptr(ReasonLargest)
	}
	if ps.reserveRoom > 0 && short != nil {
		mode := ReserveStop
		if t, _ := ps.typeNamed(h.InstanceType); h.HibernationConfigured && hibernates(t) {
			mode = ReserveHibernate
		}
		h.State, h.ReserveMode = FleetPreparing, ptr(mode)
		// It runs until it stops, so it keeps its host room.
		ps.reserveRoom--
		ps.act(FleetAction{Kind: ActionReturnToReserve, Reason: *short, Market: v.m, Host: ptr(h.ID), Mode: ptr(mode)})
		return
	}
	h.State = FleetDraining
	ps.act(FleetAction{Kind: ActionDrain, Reason: ReasonIdle, Market: v.m, Host: ptr(h.ID)})
}

// grow covers what the kept hosts leave of what this pass's load and
// headroom ask for: the parts of headroom that fit free on none of them,
// and the aggregate shortfall beyond those. The held target keeps hosts; it
// buys none. Ready reserves resume for the shortfall where a
// resume serves sooner than a launch and pays; purchases cover the rest
// once arrivals have settled. In a market whose work waits for a host,
// headroom waits too: the hosts bought for the work bring it. The floor
// that running work took is replaced only once the shortfall has lasted
// the keep window: a replacement serves no sooner than that, and work
// shorter than the window gives the room back. A shortfall beyond the
// floor is the share of a load that grew, and is bought at once, as is the
// CPU Spot market's while arrivals continue: the next starts land on it.
func (ps *pass) grow(v *marketView) {
	parts := total(v.unplaced)
	need := CoverNeed{Aggregate: v.want.Minus(v.capacity).Clamp().Minus(parts).Clamp()}
	for _, part := range v.unplaced {
		need.Items = append(need.Items, CoverItem{Shape: part, Count: 1})
	}
	if needMet(need) {
		v.holds.ShortSince = time.Time{}
		return
	}
	if v.holds.ShortSince.IsZero() {
		v.holds.ShortSince = ps.s.Now
	}
	v.shortfall = need.Aggregate.Plus(parts)
	// The kept hosts hold the floor once their work ends: it took the room.
	running := totalOf(v.kept, func(h *FleetHost) FleetCapacity { return h.Load })
	taken := v.floor.Covers(v.shortfall) && v.capacity.Covers(v.want.Minus(running).Clamp())
	switch {
	case ps.waiting[v.m]:
		v.runningReason = WaitDemand
		return
	case taken && !v.arriving && ps.s.Now.Sub(v.holds.ShortSince) < ps.p.Keep:
		v.runningReason = WaitKeep
		return
	case ps.hostRoom <= 0 || ps.room(v.m) <= 0:
		v.runningReason = ps.shortReason(v.m, shapeNeed(v.m, v.shortfall), false)
		return
	}
	for _, h := range ps.resumesFor(v, &need) {
		ps.resume(v.m, h, growReason(need), nil)
	}
	if needMet(need) {
		v.shortfall = FleetCapacity{}
		return
	}
	if ps.batching() {
		v.runningReason = WaitBatch
		return
	}
	result := ps.buyHeadroom(v, need)
	v.shortfall = need.Aggregate.Minus(result.Supplied).Clamp()
	for _, it := range result.UnmetItems {
		v.shortfall = v.shortfall.Plus(it.Shape.Times(it.Count))
	}
	if !v.shortfall.Empty() {
		v.runningReason = ps.shortReason(v.m, shapeNeed(v.m, v.shortfall), false)
	}
}

// needMet reports a need with nothing left to cover.
func needMet(need CoverNeed) bool { return len(need.Items) == 0 && need.Aggregate.Empty() }

// growReason is why a resume or purchase grows a market: its floor when
// that is what it lacks, else headroom for its load and arrivals.
func growReason(need CoverNeed) ActionReason {
	if len(need.Items) > 0 {
		return ReasonFloor
	}
	return ReasonLoad
}

// resumesFor is the market's ready reserves that resume for need, cheapest
// to serve first, each taken while it serves sooner than a launch and costs
// no more over the cost horizon than buying its share, and while a reserve
// that fits the largest shape stays behind. It takes what each covers off
// need: the parts that fit it, then its room left off the aggregate.
func (ps *pass) resumesFor(v *marketView, need *CoverNeed) []*FleetHost {
	ready := ps.inMarket(v.m, func(h FleetHost) bool { return ps.ready(h, false) })
	slices.SortStableFunc(ready, ps.retention)
	var out []*FleetHost
	for _, h := range ready {
		if needMet(*need) || ps.hostRoom <= 0 || ps.room(v.m) <= 0 {
			break
		}
		room, rest := h.Usable, make([]CoverItem, 0, len(need.Items))
		for _, it := range need.Items {
			if room.Covers(it.Shape) {
				room = room.Minus(it.Shape)
				continue
			}
			rest = append(rest, it)
		}
		if len(rest) == len(need.Items) && need.Aggregate.Empty() || !ps.leavesLarge(*h) || !ps.resumePays(h, *need) {
			continue
		}
		out = append(out, h)
		need.Items, need.Aggregate = rest, need.Aggregate.Minus(room).Clamp()
	}
	return out
}

// buyHeadroom buys the cheapest cover of need from the market's offers. A
// GPU market buys within the size rule, as its work does, and no larger
// type stands in while the sized pools cool. The CPU Spot market buys hosts
// that each hold the headroom, up to the largest shape's cap: Spot costs
// less per core on larger hosts, so a burst lands on a few large hosts
// bought ahead of it. What those cannot cover, under a quota or a
// cooldown, any offer covers.
func (ps *pass) buyHeadroom(v *marketView, need CoverNeed) CoverResult {
	r := ps.marketRanking(v.m, false)
	buy := func(offers []FleetOffer, need CoverNeed) CoverResult {
		result := Cover(offers, need, servingCost(ps.p), ps.limits(min(ps.room(v.m), ps.hostRoom), r))
		for _, node := range result.Nodes {
			holds := node.Offer.Usable.Lower(need.Aggregate)
			for i, n := range node.Placed {
				if n > 0 {
					holds = holds.Upper(need.Items[i].Shape)
				}
			}
			ps.buy(v.m, node.Offer, ActionBuy, growReason(need), holds, nil, nil)
		}
		return result
	}
	if v.m.GPU != "" {
		return buy(slices.DeleteFunc(slices.Clone(r.offers), func(o FleetOffer) bool { return !ps.p.sized(o, v.largest, v.largestDisks, r.refs, r.soon) }), need)
	}
	if !v.m.spotCPU() {
		return buy(r.offers, need)
	}
	least := v.headroom.Lower(ps.p.LargestShape.Cap)
	result := buy(slices.DeleteFunc(slices.Clone(r.offers), func(o FleetOffer) bool { return !o.Usable.Covers(least) }), need)
	if result.Complete(need) {
		return result
	}
	rest := buy(r.offers, CoverNeed{Items: result.UnmetItems, Aggregate: need.Aggregate.Minus(result.Supplied).Clamp()})
	return CoverResult{Nodes: append(result.Nodes, rest.Nodes...), UnmetItems: rest.UnmetItems, Supplied: result.Supplied.Plus(rest.Supplied)}
}

// counted reports a reserve that holds market v's stopped target: one not
// interrupted, wherever it lies and whatever agent build it stopped on. A
// reserve outside the region the market's starts read from still serves
// them, and retires first once the market holds more than it needs; one on
// an older build updates its agent when it resumes.
func (ps *pass) counted(v *marketView) func(FleetHost) bool {
	return func(h FleetHost) bool {
		return h.reserve() && !h.Protected && !v.retired[h.ID]
	}
}

// reserveSupply is what market v's counted reserves hold of its stopped
// target, the host skip aside.
func (ps *pass) reserveSupply(v *marketView, skip HostID) FleetCapacity {
	counted := ps.counted(v)
	return totalOf(ps.inMarket(v.m, func(h FleetHost) bool { return h.ID != skip && counted(h) }), func(h *FleetHost) FleetCapacity { return h.Usable })
}

// dueBackSupply is what market v's hosts due back into the reserve hold.
// A reserve purchase waits for them; a surplus host does not, and returns
// while the reserves alone fall short.
func (ps *pass) dueBackSupply(v *marketView) FleetCapacity {
	return totalOf(ps.inMarket(v.m, func(h FleetHost) bool { return ps.dueBack(v, h) }), func(h *FleetHost) FleetCapacity { return h.Usable })
}

// dueBack reports a serving or starting returnable host that returns to
// the reserve: in a market that awaits returns any, else one resumed from
// the reserve that is idle, which returns once it is surplus, or that has
// run work for less than the keep window. One busy longer is not due back,
// and the reserve buys its replacement.
func (ps *pass) dueBack(v *marketView, h FleetHost) bool {
	if !serving(h) && !starting(h) || !ps.returnable(h) {
		return false
	}
	if ps.p.Reserve(v.m).AwaitReturn {
		return true
	}
	return h.Slept && (!busy(h) && !ps.claimed[h.ID] || ps.s.Now.Sub(h.PhaseAt) < ps.p.Keep)
}

// holdsLargest reports whether a counted reserve of the market other than
// skip, or one this pass buys, fits the market's largest shape and its
// disks.
func (ps *pass) holdsLargest(v *marketView, skip HostID) bool {
	if v.largest.Empty() {
		return true
	}
	for _, h := range ps.inMarket(v.m, ps.counted(v)) {
		if h.ID != skip && ps.fitsLargest(v, *h) {
			return true
		}
	}
	return slices.ContainsFunc(ps.planned, func(n plannedHost) bool {
		return n.market == v.m && n.reserve && v.fits(n.offer.Usable, n.offer.Type)
	})
}

// reserves keeps market v's stopped target, among its reserves one that
// fits the largest shape. Interrupted reserves retire at once: they never
// resume. The rest are walked in one order, those in the region the
// market's starts read from first, the one that fits the largest shape
// next, ready ones before pending, those the market could not buy again
// before those it could, the costliest to hold last, and kept until
// the counted ready ones cover the target; the ready ones after that
// retire, and a pending one waits until it is ready to be walked as
// surplus. What the kept reserves and the hosts due back lack is bought
// once no work waits in the market and arrivals have settled.
func (ps *pass) reserves(v *marketView) {
	for _, h := range ps.inMarket(v.m, func(h FleetHost) bool { return h.resumable() && h.Protected }) {
		v.retired[h.ID] = true
		ps.reserveRoom++
		ps.act(FleetAction{Kind: ActionRetireReserve, Reason: ReasonInterrupted, Market: v.m, Host: ptr(h.ID)})
	}
	counted := ps.counted(v)
	growable := func(h *FleetHost) bool {
		_, ok := ps.typeNamed(h.InstanceType)
		return ok && !ps.cooled(*h)
	}
	candidates := ps.inMarket(v.m, func(h FleetHost) bool { return h.reserve() && !h.Protected && h.State != FleetStopping })
	slices.SortStableFunc(candidates, func(a, b *FleetHost) int {
		return cmp.Or(boolOrder(!counted(*a), !counted(*b)), boolOrder(ps.misplaced(v, a.Region), ps.misplaced(v, b.Region)),
			boolOrder(!ps.fitsLargest(v, *a), !ps.fitsLargest(v, *b)), boolOrder(!a.resumable(), !b.resumable()),
			boolOrder(growable(a), growable(b)), cmp.Compare(ps.stoppedMicros(*a), ps.stoppedMicros(*b)),
			cmp.Compare(a.Usable.CPUMillis, b.Usable.CPUMillis), strings.Compare(a.ID.String(), b.ID.String()))
	})
	target := v.holds.Stopped.Load
	var covered, ready FleetCapacity
	hasLargest, readyLargest := v.largest.Empty(), v.largest.Empty()
	for _, h := range candidates {
		if ready.Covers(target) && readyLargest {
			v.retired[h.ID] = true
			ps.reserveRoom++
			ps.act(FleetAction{Kind: ActionRetireReserve, Reason: ReasonSurplus, Market: v.m, Host: ptr(h.ID)})
			continue
		}
		if counted(*h) {
			covered = covered.Plus(h.Usable)
			hasLargest = hasLargest || ps.fitsLargest(v, *h)
			if h.resumable() {
				ready = ready.Plus(h.Usable)
				readyLargest = readyLargest || ps.fitsLargest(v, *h)
			}
		}
	}
	v.stoppedShort = target.Minus(covered).Clamp()
	var item FleetCapacity
	if !ps.holdsLargest(v, HostID{}) {
		item = v.largest
	}
	v.stoppedShort = v.stoppedShort.Upper(item)
	// What the reserves and the hosts due back lack is bought.
	short := target.Minus(ps.reserveSupply(v, HostID{})).Minus(ps.dueBackSupply(v)).Clamp()
	switch {
	case v.stoppedShort.Empty():
	case ps.waiting[v.m]:
		v.stoppedReason = WaitDemand
	case short.Empty() && !ps.lacksLargest(v):
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
			least = target.Lower(ps.p.LargestShape.Default)
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
}

// lacksLargest reports whether no counted reserve, host due back or
// purchase of this pass fits the market's largest shape.
func (ps *pass) lacksLargest(v *marketView) bool {
	if ps.holdsLargest(v, HostID{}) {
		return false
	}
	return !slices.ContainsFunc(ps.inMarket(v.m, func(h FleetHost) bool { return ps.dueBack(v, h) }), func(h *FleetHost) bool { return ps.fitsLargest(v, *h) })
}

// reserveReason settles market v's reserve shortfall after its purchases:
// held, or named by why the purchases fell short of item.
func (ps *pass) reserveReason(v *marketView, item FleetCapacity) {
	if v.stoppedShort.Empty() {
		return
	}
	v.stoppedReason = ps.shortReason(v.m, shapeNeed(v.m, item), true)
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

// rightsize replaces the kept idle host whose cheaper type pays back most:
// the hourly saving over the cost horizon must exceed what the new host
// costs while it provisions, and the new type must hold what the host holds
// of the running target. It acts only in a steady market: no work waits, no
// batch is open, nothing in it moves or became ready within the keep
// window, and the running target holds nothing above the load and headroom
// of this pass, so the kept hosts converge on the cheapest type. The same
// type in another zone never replaces a host, so a Spot price move between
// zones buys nothing.
func (ps *pass) rightsize(v *marketView) {
	moving := slices.ContainsFunc(ps.plan.Actions, func(a FleetAction) bool {
		return a.Market == v.m && a.Kind != ActionRetireReserve
	})
	coming := slices.ContainsFunc(v.kept, func(h *FleetHost) bool { return !serving(*h) || ps.s.Now.Sub(h.PhaseAt) < ps.p.Keep })
	steady := v.holds.Running.Load == v.want
	if ps.waiting[v.m] || ps.s.BatchWait > 0 || moving || coming || !steady || ps.room(v.m) <= 0 || ps.hostRoom <= 0 {
		return
	}
	var best *FleetOffer
	var replaced *FleetHost
	var bestPayback int64
	for _, h := range v.kept {
		if o, payback := ps.replacement(v, *h); o != nil && payback > bestPayback {
			best, replaced, bestPayback = o, h, payback
		}
	}
	if best != nil {
		ps.buy(v.m, *best, ActionRightsize, ReasonCheaper, ps.holdsOf(v, *replaced), nil, ptr(replaced.ID))
	}
}

// holdsOf is what kept idle host h holds of market v's running target: its
// share of the target the kept hosts without it would lack, and the parts
// of headroom no other kept host has room for.
func (ps *pass) holdsOf(v *marketView, h FleetHost) FleetCapacity {
	holds := v.want.Minus(v.capacity.Minus(h.Usable)).Clamp().Lower(h.Usable)
	var rooms []FleetCapacity
	for _, k := range v.kept {
		if k.ID != h.ID && serving(*k) && !ps.leaving(v, *k) {
			rooms = append(rooms, k.room())
		}
	}
	return holds.Upper(total(unplaced(rooms, v.wantParts)))
}

// replacement is the offer of another type that pays back most for
// replacing kept idle host h, with its payback, or nil: it holds what h
// holds of the running target, and what it saves over the cost horizon,
// priced as purchases are, exceeds what it costs while it provisions. A
// host that runs work, is an interruption's, or whose replacement EC2
// refused within the cost horizon has none.
func (ps *pass) replacement(v *marketView, h FleetHost) (*FleetOffer, int64) {
	cost, known := ps.hostCost(h)
	refused := h.RightsizeRefusedAt != nil && ps.s.Now.Sub(*h.RightsizeRefusedAt) < ps.p.CostHorizon
	if !serving(h) || busy(h) || ps.claimed[h.ID] || h.Protected || refused || !known {
		return nil, 0
	}
	need := ps.holdsOf(v, h)
	if need.Empty() {
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

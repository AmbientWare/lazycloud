package compute

import (
	"cmp"
	"slices"
	"strconv"
)

// coverWidth bounds the partial covers kept after each added node.
const coverWidth = 128

// CoverItem is Count requests of one shape, each attaching Disks disks; each
// goes whole onto one node and takes a disk slot of its node per disk.
type CoverItem struct {
	Shape FleetCapacity
	Disks int
	Count int
}

// fits is how many of the item, up to limit, fit in free room and free disk
// slots.
func (i CoverItem) fits(free FleetCapacity, disks, limit int) int {
	if i.Disks > 0 {
		limit = min(limit, disks/i.Disks)
	}
	return free.fits(i.Shape, limit)
}

// CoverNeed is what a set of new nodes must supply: items placed whole and
// aggregate room beyond the items.
type CoverNeed struct {
	Items     []CoverItem
	Aggregate FleetCapacity
}

// CoverNode is one node to buy and how many of each item it takes, by the
// need's item index.
type CoverNode struct {
	Offer  FleetOffer
	Placed []int
}

// CoverLimits bound a cover: at most Nodes nodes, within the vCPUs each
// known quota in VCPUs has left, each node of an offer Sized admits for
// what it holds: the items placed on it, the aggregate it supplies and the
// disks they attach. A nil Sized admits any.
type CoverLimits struct {
	Nodes int
	VCPUs map[QuotaKey]int64
	Sized func(o FleetOffer, holds FleetCapacity, disks int) bool
}

// CoverResult is the chosen nodes and what they leave unmet.
type CoverResult struct {
	Nodes      []CoverNode
	UnmetItems []CoverItem
	Supplied   FleetCapacity
}

// Complete reports whether the nodes meet the whole need.
func (r CoverResult) Complete(need CoverNeed) bool {
	return len(r.UnmetItems) == 0 && r.Supplied.Covers(need.Aggregate)
}

type coverState struct {
	cost      int64
	remaining []int
	supplied  FleetCapacity
	nodes     []int
	placed    [][]int
	progress  float64
	// quota is the vCPUs chosen per limited quota, by quotas index.
	quota []int64
}

// Cover picks nodes from offers within limits that meet need at the
// lowest total cost. It is a bounded beam search: after each added node it
// keeps the coverWidth partial covers with the lowest cost per unit of
// progress. A complete cover minimizes cost among those explored; without
// one, the cover that places the most returns with what is unmet.
func Cover(offers []FleetOffer, need CoverNeed, cost func(FleetOffer) int64, limits CoverLimits) CoverResult {
	candidates := coverCandidates(offers, need, cost, limits)
	var quotas []QuotaKey
	for key := range limits.VCPUs {
		if slices.ContainsFunc(candidates, func(o FleetOffer) bool { return o.Quota == key }) {
			quotas = append(quotas, key)
		}
	}
	order := make([]int, len(need.Items))
	for i := range order {
		order[i] = i
	}
	slices.SortStableFunc(order, func(a, b int) int { return shapeOrder(need.Items[b].Shape, need.Items[a].Shape) })
	start := coverState{remaining: make([]int, len(need.Items)), quota: make([]int64, len(quotas))}
	for i, item := range need.Items {
		start.remaining[i] = item.Count
	}
	total := 0
	for _, item := range need.Items {
		total += item.Count
	}
	states := []coverState{start}
	partial := start
	var best *coverState
	for depth := 0; depth < limits.Nodes && len(states) > 0; depth++ {
		expanded := map[string]coverState{}
		for _, s := range states {
			for c, o := range candidates {
				q := slices.Index(quotas, o.Quota)
				if q >= 0 && s.quota[q]+o.Type.VCPUs() > limits.VCPUs[o.Quota] {
					continue
				}
				next, moved := s.add(c, o, cost(o), need, order, total, limits.Sized)
				if q >= 0 {
					next.quota[q] += o.Type.VCPUs()
				}
				if !moved || (best != nil && next.cost >= best.cost) {
					continue
				}
				if next.complete(need) {
					best = &next
					continue
				}
				key := next.key()
				if prev, ok := expanded[key]; !ok || next.cost < prev.cost {
					expanded[key] = next
				}
			}
		}
		states = states[:0]
		for _, s := range expanded {
			states = append(states, s)
		}
		slices.SortFunc(states, func(a, b coverState) int {
			return cmp.Or(cmp.Compare(a.ratio(), b.ratio()), cmp.Compare(a.cost, b.cost), slices.Compare(a.nodes, b.nodes))
		})
		states = states[:min(len(states), coverWidth)]
		for _, s := range states {
			if s.betterPartial(partial) {
				partial = s
			}
		}
	}
	chosen := partial
	if best != nil {
		chosen = *best
	}
	return chosen.result(candidates, need)
}

// coverCandidates are the offers that can contribute, the cheapest of each
// usable shape and limited quota, ties to the earlier offer.
func coverCandidates(offers []FleetOffer, need CoverNeed, cost func(FleetOffer) int64, limits CoverLimits) []FleetOffer {
	var out []FleetOffer
	for _, o := range offers {
		if need.Aggregate.Empty() && !slices.ContainsFunc(need.Items, func(i CoverItem) bool {
			return i.fits(o.Usable, o.Type.DiskSlots(), 1) > 0
		}) {
			continue
		}
		_, limited := limits.VCPUs[o.Quota]
		n := slices.IndexFunc(out, func(c FleetOffer) bool {
			_, cLimited := limits.VCPUs[c.Quota]
			return c.Usable == o.Usable && (!limited && !cLimited || c.Quota == o.Quota)
		})
		switch {
		case n < 0:
			out = append(out, o)
		case cost(o) < cost(out[n]):
			out[n] = o
		}
	}
	return out
}

// add places what it can on one more node of offer c; a node sized
// rejects for what it would hold moves nothing.
func (s coverState) add(c int, o FleetOffer, price int64, need CoverNeed, order []int, total int,
	sized func(FleetOffer, FleetCapacity, int) bool) (coverState, bool) {
	free, disks := o.Usable, o.Type.DiskSlots()
	placed := make([]int, len(need.Items))
	remaining := slices.Clone(s.remaining)
	moved := false
	for _, i := range order {
		item := need.Items[i]
		if n := item.fits(free, disks, remaining[i]); n > 0 {
			placed[i], remaining[i] = n, remaining[i]-n
			free, disks = free.Minus(item.Shape.Times(n)), disks-n*item.Disks
			moved = true
		}
	}
	supplied := s.supplied.Plus(free).Lower(need.Aggregate)
	moved = moved || supplied != s.supplied
	if sized != nil && moved && !sized(o, o.Usable.Minus(free).Plus(supplied.Minus(s.supplied)), o.Type.DiskSlots()-disks) {
		moved = false
	}
	next := coverState{
		cost: s.cost + price, remaining: remaining, supplied: supplied,
		nodes: append(slices.Clone(s.nodes), c), placed: append(slices.Clone(s.placed), placed), quota: slices.Clone(s.quota),
	}
	next.progress = next.coverage(need, total)
	return next, moved
}

// coverage is the items placed and the share of each aggregate dimension
// supplied.
func (s coverState) coverage(need CoverNeed, total int) float64 {
	left := 0
	for _, n := range s.remaining {
		left += n
	}
	progress := float64(total - left)
	for _, d := range [][2]int64{
		{int64(s.supplied.CPUMillis), int64(need.Aggregate.CPUMillis)},
		{s.supplied.MemoryBytes, need.Aggregate.MemoryBytes},
		{int64(s.supplied.GPUs), int64(need.Aggregate.GPUs)},
	} {
		if d[1] > 0 {
			progress += float64(d[0]) / float64(d[1])
		}
	}
	return progress
}

func (s coverState) ratio() float64 { return float64(s.cost) / max(s.progress, 0.001) }

func (s coverState) complete(need CoverNeed) bool {
	for _, n := range s.remaining {
		if n > 0 {
			return false
		}
	}
	return s.supplied.Covers(need.Aggregate)
}

func (s coverState) key() string {
	b := make([]byte, 0, 16*len(s.remaining)+64)
	for _, n := range s.remaining {
		b = strconv.AppendInt(append(b, ','), int64(n), 10)
	}
	b = strconv.AppendInt(append(b, '|'), int64(s.supplied.CPUMillis), 10)
	b = strconv.AppendInt(append(b, ','), s.supplied.MemoryBytes, 10)
	b = strconv.AppendInt(append(b, ','), int64(s.supplied.GPUs), 10)
	for _, v := range s.quota {
		b = strconv.AppendInt(append(b, ','), v, 10)
	}
	return string(b)
}

// betterPartial prefers more items placed, then more aggregate supplied,
// then lower cost.
func (s coverState) betterPartial(other coverState) bool {
	sum := func(v []int) int {
		n := 0
		for _, x := range v {
			n += x
		}
		return n
	}
	return cmp.Or(
		cmp.Compare(sum(other.remaining), sum(s.remaining)),
		cmp.Compare(s.supplied.CPUMillis, other.supplied.CPUMillis),
		cmp.Compare(s.supplied.MemoryBytes, other.supplied.MemoryBytes),
		cmp.Compare(s.supplied.GPUs, other.supplied.GPUs),
		cmp.Compare(other.cost, s.cost),
	) > 0
}

func (s coverState) result(candidates []FleetOffer, need CoverNeed) CoverResult {
	r := CoverResult{Supplied: s.supplied}
	for n, c := range s.nodes {
		r.Nodes = append(r.Nodes, CoverNode{Offer: candidates[c], Placed: s.placed[n]})
	}
	for i, left := range s.remaining {
		if left > 0 {
			item := need.Items[i]
			item.Count = left
			r.UnmetItems = append(r.UnmetItems, item)
		}
	}
	return r
}

// shapeOrder sorts shapes by GPUs, then memory, then CPU.
func shapeOrder(a, b FleetCapacity) int {
	return cmp.Or(cmp.Compare(a.GPUs, b.GPUs), cmp.Compare(a.MemoryBytes, b.MemoryBytes), cmp.Compare(a.CPUMillis, b.CPUMillis))
}

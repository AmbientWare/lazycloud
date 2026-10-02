package compute

import (
	"cmp"
	"math/bits"
	"slices"
	"strconv"
)

// coverWidth bounds the partial covers kept after each added node.
const coverWidth = 128

// coverShapes bounds the shapes one cover tracks; more merge into one
// covering shape.
const coverShapes = 64

// CoverItem is Count requests of one shape; each goes whole onto one node.
type CoverItem struct {
	Shape FleetCapacity
	Count int
}

// CoverNeed is what a set of new nodes must supply: items placed whole,
// aggregate room beyond the items, and shapes that must each fit an empty
// chosen node.
type CoverNeed struct {
	Items     []CoverItem
	Aggregate FleetCapacity
	Shapes    []FleetCapacity
}

// CoverNode is one node to buy and how many of each item it takes, by the
// need's item index.
type CoverNode struct {
	Offer  FleetOffer
	Placed []int
}

// CoverResult is the chosen nodes and what they leave unmet.
type CoverResult struct {
	Nodes       []CoverNode
	UnmetItems  []CoverItem
	Supplied    FleetCapacity
	UnmetShapes []FleetCapacity
}

// Complete reports whether the nodes meet the whole need.
func (r CoverResult) Complete(need CoverNeed) bool {
	return len(r.UnmetItems) == 0 && len(r.UnmetShapes) == 0 && r.Supplied.Covers(need.Aggregate)
}

type coverState struct {
	cost      int64
	remaining []int
	supplied  FleetCapacity
	unmet     uint64
	nodes     []int
	placed    [][]int
	progress  float64
}

// Cover picks at most maxNodes nodes from offers that meet need at the
// lowest total cost. It is a bounded beam search: after each added node it
// keeps the coverWidth partial covers with the lowest cost per unit of
// progress. A complete cover minimizes cost among those explored; without
// one, the cover that places the most returns with what is unmet.
func Cover(offers []FleetOffer, need CoverNeed, cost func(FleetOffer) int64, maxNodes int) CoverResult {
	need.Shapes = mergeShapes(need.Shapes, coverShapes)
	candidates := coverCandidates(offers, need, cost)
	order := make([]int, len(need.Items))
	for i := range order {
		order[i] = i
	}
	slices.SortStableFunc(order, func(a, b int) int { return shapeOrder(need.Items[b].Shape, need.Items[a].Shape) })
	start := coverState{remaining: make([]int, len(need.Items)), unmet: uint64(1)<<len(need.Shapes) - 1}
	if len(need.Shapes) == coverShapes {
		start.unmet = ^uint64(0)
	}
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
	for depth := 0; depth < maxNodes && len(states) > 0; depth++ {
		expanded := map[string]coverState{}
		for _, s := range states {
			for c, o := range candidates {
				next, moved := s.add(c, o, cost(o), need, order, total)
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
// usable shape, ties to the earlier offer.
func coverCandidates(offers []FleetOffer, need CoverNeed, cost func(FleetOffer) int64) []FleetOffer {
	var out []FleetOffer
	for _, o := range offers {
		useful := !need.Aggregate.Empty() ||
			slices.ContainsFunc(need.Items, func(i CoverItem) bool { return o.Usable.Covers(i.Shape) }) ||
			slices.ContainsFunc(need.Shapes, func(s FleetCapacity) bool { return o.Usable.Covers(s) })
		if !useful {
			continue
		}
		n := slices.IndexFunc(out, func(c FleetOffer) bool { return c.Usable == o.Usable })
		switch {
		case n < 0:
			out = append(out, o)
		case cost(o) < cost(out[n]):
			out[n] = o
		}
	}
	return out
}

// add places what it can on one more node of offer c.
func (s coverState) add(c int, o FleetOffer, price int64, need CoverNeed, order []int, total int) (coverState, bool) {
	free := o.Usable
	placed := make([]int, len(need.Items))
	remaining := slices.Clone(s.remaining)
	moved := false
	for _, i := range order {
		if n := free.fits(need.Items[i].Shape, remaining[i]); n > 0 {
			placed[i], remaining[i] = n, remaining[i]-n
			free = free.Minus(need.Items[i].Shape.Times(n))
			moved = true
		}
	}
	supplied := s.supplied.Plus(free).Lower(need.Aggregate)
	unmet := s.unmet
	for i, shape := range need.Shapes {
		if o.Usable.Covers(shape) {
			unmet &^= 1 << i
		}
	}
	moved = moved || supplied != s.supplied || unmet != s.unmet
	next := coverState{
		cost: s.cost + price, remaining: remaining, supplied: supplied, unmet: unmet,
		nodes: append(slices.Clone(s.nodes), c), placed: append(slices.Clone(s.placed), placed),
	}
	next.progress = next.coverage(need, total)
	return next, moved
}

// coverage is the items placed, the shapes covered and the share of each
// aggregate dimension supplied.
func (s coverState) coverage(need CoverNeed, total int) float64 {
	left := 0
	for _, n := range s.remaining {
		left += n
	}
	progress := float64(total-left) + float64(len(need.Shapes)-bits.OnesCount64(s.unmet&(uint64(1)<<len(need.Shapes)-1)))
	for _, d := range [][2]int64{
		{s.supplied.CPUMillis, need.Aggregate.CPUMillis},
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
	return s.unmet&(uint64(1)<<len(need.Shapes)-1) == 0 && !slices.ContainsFunc(s.remaining, func(n int) bool { return n > 0 }) &&
		s.supplied.Covers(need.Aggregate)
}

func (s coverState) key() string {
	b := make([]byte, 0, 16*len(s.remaining)+64)
	for _, n := range s.remaining {
		b = strconv.AppendInt(append(b, ','), int64(n), 10)
	}
	b = strconv.AppendInt(append(b, '|'), s.supplied.CPUMillis, 10)
	b = strconv.AppendInt(append(b, ','), s.supplied.MemoryBytes, 10)
	b = strconv.AppendInt(append(b, ','), int64(s.supplied.GPUs), 10)
	b = strconv.AppendUint(append(b, '|'), s.unmet, 16)
	return string(b)
}

// betterPartial prefers more items placed, then fewer shapes unmet, then
// more aggregate supplied, then lower cost.
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
		cmp.Compare(bits.OnesCount64(other.unmet), bits.OnesCount64(s.unmet)),
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
			r.UnmetItems = append(r.UnmetItems, CoverItem{Shape: need.Items[i].Shape, Count: left})
		}
	}
	for i, shape := range need.Shapes {
		if s.unmet&(1<<i) != 0 {
			r.UnmetShapes = append(r.UnmetShapes, shape)
		}
	}
	return r
}

// shapeOrder sorts shapes by GPUs, then memory, then CPU.
func shapeOrder(a, b FleetCapacity) int {
	return cmp.Or(cmp.Compare(a.GPUs, b.GPUs), cmp.Compare(a.MemoryBytes, b.MemoryBytes), cmp.Compare(a.CPUMillis, b.CPUMillis))
}

// mergeShapes keeps at most limit distinct shapes, largest first; the rest
// merge into one shape that covers each of them. The merged shape may
// overstate a request but never hides one that must fit.
func mergeShapes(shapes []FleetCapacity, limit int) []FleetCapacity {
	var distinct []FleetCapacity
	for _, s := range shapes {
		if !s.Empty() && !slices.Contains(distinct, s) {
			distinct = append(distinct, s)
		}
	}
	slices.SortFunc(distinct, func(a, b FleetCapacity) int { return shapeOrder(b, a) })
	if len(distinct) <= limit {
		return distinct
	}
	merged := distinct[limit-1]
	for _, s := range distinct[limit:] {
		merged = merged.Upper(s)
	}
	return append(distinct[:limit-1:limit-1], merged)
}

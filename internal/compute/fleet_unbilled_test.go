package compute

import (
	"maps"
	"slices"
	"time"
)

// Unbilled capacity buckets: why a running host's capacity bills nothing.
const (
	bucketBilled        = "billed"
	bucketBooting       = "booting"
	bucketWarm          = "warm headroom"
	bucketFragmentation = "fragmentation"
	bucketIdle          = "drain and idle tail"
	bucketPreparing     = "reserve preparation"
	bucketInterrupted   = "interrupted"
	bucketOther         = "other"
)

var buckets = []string{bucketBilled, bucketBooting, bucketWarm, bucketFragmentation, bucketIdle, bucketPreparing, bucketInterrupted, bucketOther}

// capacityUse is CPU seconds, GiB seconds and µ$·s of host spend.
type capacityUse struct{ cpu, gib, micros float64 }

// slotHolding is the room each serving host's warm slots hold in a pass,
// from the same steps PlanFleet takes up to its cover.
func slotHolding(p Policy, s FleetSnapshot) map[HostID]FleetCapacity {
	ps := &pass{
		p: p, s: s, hosts: slices.Clone(s.Hosts),
		used: map[ReserveMarket]int{}, waiting: map[ReserveMarket]bool{}, limited: map[ReserveMarket]bool{}, batched: map[ReserveMarket]bool{},
		hostRoom: s.HostRoom, reserveRoom: s.ReserveRoom, offers: map[string][]FleetOffer{}, byMarket: map[ReserveMarket]*marketView{},
		claimed: map[HostID]bool{}, holding: map[HostID][]warmSlot{}, plan: FleetPlan{IdleSince: map[HostID]time.Time{}},
		quotaUsed: QuotaUse(s.Hosts, s.Offers.Catalog),
	}
	ps.s.Offers.QuotaUsed = maps.Clone(ps.quotaUsed)
	items := ps.pendingItems()
	views := ps.views(items)
	ps.cover(slices.Concat(items, slotItems(views)))
	out := map[HostID]FleetCapacity{}
	for id := range ps.holding {
		out[id] = ps.held(id)
	}
	return out
}

// attribute splits one running host's capacity for one tick of secs
// seconds into buckets, by CPU and memory, and its spend by CPU share.
func (s *sim) attribute(h *simHost, secs float64) {
	if s.r.unbilled == nil {
		s.r.unbilled = map[string]*capacityUse{}
		for _, b := range buckets {
			s.r.unbilled[b] = &capacityUse{}
		}
	}
	fh := s.fleetHost(h)
	usable := h.Usable
	add := func(b string, c FleetCapacity) {
		if c.CPUMillis <= 0 && c.MemoryBytes <= 0 {
			return
		}
		u := s.r.unbilled[b]
		u.cpu += float64(max(c.CPUMillis, 0)) / 1000 * secs
		u.gib += float64(max(c.MemoryBytes, 0)) / float64(gib) * secs
		u.micros += float64(h.offer.HourlyMicros) * secs * float64(max(c.CPUMillis, 0)) / float64(usable.CPUMillis)
	}
	load := fh.Load.Lower(usable)
	free := usable.Minus(load).Clamp()
	add(bucketBilled, load)
	switch {
	case h.failAt != nil:
		add(bucketOther, free)
	case h.State == FleetStarting:
		add(bucketBooting, free)
	case h.State == FleetPreparing || h.State == FleetStopping:
		add(bucketPreparing, free)
	case h.reclaimAt != nil:
		add(bucketInterrupted, free)
	case h.State == FleetDraining:
		add(bucketIdle, free)
	case h.State == FleetServing:
		warm := free.Lower(s.held[h.ID])
		add(bucketWarm, warm)
		rest := free.Minus(warm).Clamp()
		if len(h.containers) == 0 {
			add(bucketIdle, rest)
		} else {
			add(bucketFragmentation, rest)
		}
	default:
		add(bucketOther, free)
	}
}

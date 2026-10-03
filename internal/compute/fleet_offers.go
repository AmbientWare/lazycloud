package compute

import (
	"cmp"
	"slices"
	"strings"
	"time"

	"github.com/AmbientWare/lazycloud/internal/billing"
)

// OfferCooldown is a refusal that cools one offer until Until. A quota
// refusal cools every type of the offer's quota class in its region and
// market.
type OfferCooldown struct {
	Region       string
	InstanceType string
	Market       Market
	RefusedAt    time.Time
	Until        time.Time
	Quota        bool
}

// QuotaKey names one EC2 vCPU quota: a class of types in a region and
// market.
type QuotaKey struct {
	Region string
	Class  QuotaClass
	Market Market
}

// VCPUQuota is an EC2 vCPU quota's value as Service Quotas last reported
// it. A quota never read is no limit.
type VCPUQuota struct {
	Key   QuotaKey
	VCPUs int64
}

// QuotaUse counts the vCPUs hosts count against each quota: every instance
// EC2 may run, which excludes stopped reserves.
func QuotaUse(hosts []FleetHost, catalog []CatalogType) map[QuotaKey]int64 {
	used := map[QuotaKey]int64{}
	for _, h := range hosts {
		if h.resumable() {
			continue
		}
		if i := slices.IndexFunc(catalog, func(t CatalogType) bool { return t.Name == h.InstanceType }); i >= 0 {
			class, _ := QuotaClassOf(h.InstanceType)
			used[QuotaKey{Region: h.Region, Class: class, Market: h.Market}] += catalog[i].VCPUs()
		}
	}
	return used
}

// quotaRoom is what each known quota leaves after used.
func quotaRoom(quotas []VCPUQuota, used map[QuotaKey]int64) map[QuotaKey]int64 {
	room := map[QuotaKey]int64{}
	for _, q := range quotas {
		room[q.Key] = q.VCPUs - used[q.Key]
	}
	return room
}

// OfferInputs are what offers are ranked from.
type OfferInputs struct {
	Now     time.Time
	Catalog []CatalogType
	// Networks are the platform's launchable regions.
	Networks  map[string]Network
	Spot      []SpotQuote
	Cooldowns []OfferCooldown
	// Rates are the platform fleet's compute rates in force at Now.
	Rates []billing.ComputeRate
	// ReportedMemory is the memory hosts of each type advertise, once one
	// has reported.
	ReportedMemory map[string]int64
	// ZoneHosts counts live hosts per availability zone id.
	ZoneHosts map[string]int
	// ZoneTypes are the types EC2 offers in each zone, by region and zone
	// id. A zone of a region it holds offers only what it lists; a region
	// it lacks limits nothing.
	ZoneTypes map[string]map[string][]string
	// Quotas are the EC2 vCPU quotas and QuotaUsed what live hosts count
	// against them.
	Quotas    []VCPUQuota
	QuotaUsed map[QuotaKey]int64
	// OwnerPays is set for a connected account's offers: the account pays
	// its own hosts, so no purchase margin applies.
	OwnerPays bool
}

// FleetOffer is one way to buy a host: a type in a zone and market.
type FleetOffer struct {
	Type   CatalogType
	Region string
	Zone   string
	ZoneID string
	Market Market
	Usable FleetCapacity
	// Hibernate is set on a reserve offer whose host hibernates.
	Hibernate bool
	// HourlyMicros is compute, root disk and public IPv4 while running;
	// StoppedMicros is the root disk alone, what a stopped reserve costs.
	HourlyMicros  int64
	StoppedMicros int64
	// CoolingRegion marks an offer in a region with recent refusals; it
	// ranks last and serves only when nothing else does.
	CoolingRegion bool
	// Quota is the vCPU quota a host bought from the offer counts against.
	Quota QuotaKey
}

// Key names the offer.
func (o FleetOffer) Key() string {
	return o.Region + "/" + o.ZoneID + "/" + o.Type.Name + "/" + string(o.Market)
}

// servingCost prices a host bought to serve over the cost horizon.
func servingCost(p Policy) func(FleetOffer) int64 {
	return func(o FleetOffer) int64 { return o.HourlyMicros * int64(p.CostHorizon/time.Second) }
}

// reserveCost prices a reserve: held stopped over the cost horizon, after
// running while it is prepared.
func reserveCost(p Policy) func(FleetOffer) int64 {
	return func(o FleetOffer) int64 {
		return o.StoppedMicros*int64(p.CostHorizon/time.Second) + o.HourlyMicros*int64(p.Provision/time.Second)
	}
}

// purchaseRejection says why the margin check refused an offer.
type purchaseRejection string

const (
	rejectUnpricedCapacity   purchaseRejection = "capacity has no applicable customer rate"
	rejectInsufficientMargin purchaseRejection = "supplier cost exceeds the fleet purchase margin limit"
)

type rateIndex map[billing.RateClass]map[billing.GPUType]billing.ComputeRate

func indexRates(rates []billing.ComputeRate) rateIndex {
	index := rateIndex{}
	for _, r := range rates {
		if index[r.Class] == nil {
			index[r.Class] = map[billing.GPUType]billing.ComputeRate{}
		}
		index[r.Class][r.GPU] = r
	}
	return index
}

// marginRejection checks an offer's complete cost against at most
// 100-MarginPercent of what its usable resources earn at the rate card.
// Spot-tolerant work keeps its lower rate on an on-demand host. Revenue is
// in nanodollars an hour, cost in microdollars.
func marginRejection(p Policy, rates rateIndex, o FleetOffer, preemptible bool) (purchaseRejection, bool) {
	class := billing.RateClassFor(false, preemptible || o.Market == MarketSpot)
	rate, ok := rates[class][billing.GPUType(o.Type.GPU)]
	if !ok || o.Usable.CPUMillis <= 0 || o.Usable.MemoryBytes <= 0 {
		return rejectUnpricedCapacity, true
	}
	revenue := o.Usable.CPUMillis*rate.CPUCoreHour/1000 +
		o.Usable.MemoryBytes/gib*rate.MemoryGiBHour + o.Usable.MemoryBytes%gib*rate.MemoryGiBHour/gib +
		int64(o.Usable.GPUs)*rate.GPUCardHour
	if revenue <= 0 {
		return rejectUnpricedCapacity, true
	}
	if o.HourlyMicros > revenue*(100-p.MarginPercent)/100_000 {
		return rejectInsufficientMargin, true
	}
	return "", false
}

// coolingRegions are the regions where refusals from at least
// RegionFailures distinct offers fell within RegionFailureWindow.
func coolingRegions(p Policy, cooldowns []OfferCooldown, now time.Time) map[string]bool {
	offers := map[string]map[string]bool{}
	for _, c := range cooldowns {
		if c.RefusedAt.Before(now.Add(-p.RegionFailureWindow)) {
			continue
		}
		if offers[c.Region] == nil {
			offers[c.Region] = map[string]bool{}
		}
		offers[c.Region][c.InstanceType+"/"+string(c.Market)] = true
	}
	cooling := map[string]bool{}
	for region, refused := range offers {
		cooling[region] = len(refused) >= p.RegionFailures
	}
	return cooling
}

// cooled reports whether a cooldown holds an offer back at now.
func cooled(cooldowns []OfferCooldown, now time.Time, region, instanceType string, market Market) bool {
	class, _ := QuotaClassOf(instanceType)
	return slices.ContainsFunc(cooldowns, func(c OfferCooldown) bool {
		if c.Region != region || c.Market != market || !c.Until.After(now) {
			return false
		}
		cooledClass, ok := QuotaClassOf(c.InstanceType)
		return c.InstanceType == instanceType || (c.Quota && ok && cooledClass == class)
	})
}

// spotPrice is the freshest quote for a type in a zone within the policy's
// age, if any.
func spotPrice(p Policy, quotes []SpotQuote, now time.Time, region, zoneID, instanceType string) (int64, bool) {
	var best *SpotQuote
	for i, q := range quotes {
		if q.Region != region || q.ZoneID != zoneID || q.InstanceType != instanceType || now.Sub(q.ObservedAt) > p.SpotPriceAge {
			continue
		}
		if best == nil || q.ObservedAt.After(best.ObservedAt) {
			best = &quotes[i]
		}
	}
	if best == nil {
		return 0, false
	}
	return best.HourlyMicros, true
}

// RankOffers lists the offers that can host need, best first. A reserve
// offer prices the root disk a hibernating reserve needs. It keeps offers
// sold in the region, in a market the need allows, not cooling, with a
// complete cost, whose usable capacity covers the need, whose GPUs the need
// accepts (GPU hosts only for GPU work), and that keep the purchase margin
// unless the owner pays. An on-demand reserve offer hibernates where the
// type does (see hibernates).
// Order: the need's GPU preference, cooling regions last, complete hourly
// cost, region preference, fewest hosts in the zone, key. An offer whose
// host would exceed a known vCPU quota is skipped.
func RankOffers(p Policy, need Requirement, reserve bool, in OfferInputs) []FleetOffer {
	rates := indexRates(in.Rates)
	room := quotaRoom(in.Quotas, in.QuotaUsed)
	cooling := coolingRegions(p, in.Cooldowns, in.Now)
	gpus := need.GPUsNeeded()
	markets := []Market{MarketOnDemand}
	if need.Preemptible {
		markets = []Market{MarketSpot, MarketOnDemand}
	}
	var offers []FleetOffer
	for _, region := range regionOrder() {
		network, ok := in.Networks[region]
		if !ok || (need.Region != "" && ProductRegion(region) != need.Region) {
			continue
		}
		for _, subnet := range network.Subnets {
			if need.Zone != "" && subnet.Zone != need.Zone && subnet.ZoneID != need.Zone {
				continue
			}
			for _, t := range in.Catalog {
				onDemand, sold := t.OnDemandMicros(region)
				if zones, known := in.ZoneTypes[region]; known && !slices.Contains(zones[subnet.ZoneID], t.Name) {
					continue
				}
				if !sold || (gpus > 0) != (t.GPUCount > 0) || t.GPUCount < gpus || (gpus > 0 && !GPUAccepted(need.GPUs, t.GPU)) {
					continue
				}
				usable := t.Usable(in.ReportedMemory[t.Name])
				if !usable.Covers(FleetCapacity{CPUMillis: need.CPUMillis, MemoryBytes: need.MemoryBytes, GPUs: gpus}) {
					continue
				}
				for _, market := range markets {
					hibernate := reserve && hibernates(t, market)
					disk := rootDiskMicros(region, t.RootGiB(hibernate))
					compute := onDemand
					if market == MarketSpot {
						if compute, ok = spotPrice(p, in.Spot, in.Now, region, subnet.ZoneID, t.Name); !ok {
							continue
						}
					}
					class, _ := QuotaClassOf(t.Name)
					quota := QuotaKey{Region: region, Class: class, Market: market}
					if left, known := room[quota]; cooled(in.Cooldowns, in.Now, region, t.Name, market) || (known && left < t.VCPUs()) {
						continue
					}
					o := FleetOffer{
						Type: t, Region: region, Zone: subnet.Zone, ZoneID: subnet.ZoneID, Market: market, Usable: usable,
						Hibernate: hibernate, HourlyMicros: compute + disk + ratesIn(region).ipv4Hour, StoppedMicros: disk,
						CoolingRegion: cooling[region], Quota: quota,
					}
					if _, rejected := marginRejection(p, rates, o, need.Preemptible); rejected && !in.OwnerPays {
						continue
					}
					offers = append(offers, o)
				}
			}
		}
	}
	slices.SortStableFunc(offers, func(a, b FleetOffer) int {
		return cmp.Or(
			cmp.Compare(GPURank(need.GPUs, a.Type.GPU), GPURank(need.GPUs, b.Type.GPU)),
			boolOrder(a.CoolingRegion, b.CoolingRegion),
			cmp.Compare(a.HourlyMicros, b.HourlyMicros),
			cmp.Compare(slices.Index(regionOrder(), a.Region), slices.Index(regionOrder(), b.Region)),
			cmp.Compare(in.ZoneHosts[a.ZoneID], in.ZoneHosts[b.ZoneID]),
			strings.Compare(a.Key(), b.Key()),
		)
	})
	return offers
}

// hibernates reports whether a reserve of type t bought in market sleeps by
// hibernating. Spot reserves stop plainly: in us-east-2 a Spot c6a.2xlarge
// hibernation on 2026-09-27 never reached stopped and was forced, while an
// on-demand c6a.2xlarge hibernated in the same call, and a Spot m6a.8xlarge
// was forced after 11 minutes on 2026-09-28. A forced stop loses the image
// after holding the host for providerDeadline.
func hibernates(t CatalogType, market Market) bool {
	return t.Hibernates && market == MarketOnDemand
}

// preferHealthy drops offers in cooling regions while another offer
// remains. Callers apply it after every placement filter, so a cooling
// region still serves demand nothing else can.
func preferHealthy(offers []FleetOffer) []FleetOffer {
	if !slices.ContainsFunc(offers, func(o FleetOffer) bool { return !o.CoolingRegion }) {
		return offers
	}
	return slices.DeleteFunc(slices.Clone(offers), func(o FleetOffer) bool { return o.CoolingRegion })
}

// boolOrder sorts false before true.
func boolOrder(a, b bool) int {
	switch {
	case a == b:
		return 0
	case a:
		return 1
	}
	return -1
}

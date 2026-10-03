package compute

import (
	"slices"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/billing"
)

var offerNow = time.Date(2026, 10, 2, 12, 0, 0, 0, time.UTC)

func oneZone(zone, zoneID string) Network {
	return Network{Subnets: []Subnet{{ID: "subnet-" + zoneID, Zone: zone, ZoneID: zoneID}}}
}

func fleetRates(t *testing.T) []billing.ComputeRate {
	t.Helper()
	rates, err := billing.FleetComputeRates(offerNow)
	if err != nil {
		t.Fatal(err)
	}
	return rates
}

func offerInputs(t *testing.T) OfferInputs {
	return OfferInputs{
		Now: offerNow, Catalog: FleetCatalog(), Rates: fleetRates(t),
		Networks: map[string]Network{"us-east-2": oneZone("us-east-2a", "use2-az1")},
	}
}

// A request of every vCPU of an AWS size buys that size, not the next one up.
func TestARequestOfEveryVCPUBuysThatSize(t *testing.T) {
	in := offerInputs(t)
	for _, vcpus := range []int64{8, 16} {
		offers := RankOffers(DefaultPolicy(), Requirement{CPUMillis: vcpus * 1000, MemoryBytes: 8 * gib}, false, in)
		if len(offers) == 0 || offers[0].Type.CPUMillis != vcpus*1000 {
			t.Fatalf("%d vCPU request: offers %v", vcpus, offerKeys(offers))
		}
	}
}

func TestOffersRespectInterruptionToleranceAndZone(t *testing.T) {
	in := offerInputs(t)
	in.Networks = map[string]Network{"us-east-2": {Subnets: []Subnet{
		{ID: "a", Zone: "us-east-2a", ZoneID: "use2-az1"}, {ID: "b", Zone: "us-east-2b", ZoneID: "use2-az2"},
	}}}
	in.Spot = []SpotQuote{
		{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: "m7i.2xlarge", HourlyMicros: 150_000, ObservedAt: offerNow},
		{Region: "us-east-2", ZoneID: "use2-az2", InstanceType: "m7i.2xlarge", HourlyMicros: 140_000, ObservedAt: offerNow.Add(-2 * time.Hour)},
	}
	in.Catalog = []CatalogType{mustType(t, "m7i.2xlarge")}
	need := Requirement{CPUMillis: 4000, MemoryBytes: 8 * gib}
	for _, o := range RankOffers(DefaultPolicy(), need, false, in) {
		if o.Market == MarketSpot {
			t.Fatalf("non-preemptible work offered Spot %s", o.Key())
		}
	}
	need.Preemptible = true
	// At the rate card, Spot-tolerant work earns too little to pay for an
	// on-demand m7i.2xlarge, and a stale quote prices nothing.
	offers := RankOffers(DefaultPolicy(), need, false, in)
	if len(offers) != 1 || offers[0].Key() != "us-east-2/use2-az1/m7i.2xlarge/spot" {
		t.Fatalf("preemptible offers %v", offerKeys(offers))
	}
	need.Zone = "use2-az2"
	for _, o := range RankOffers(DefaultPolicy(), need, false, in) {
		if o.ZoneID != "use2-az2" {
			t.Fatalf("zone-pinned work offered %s", o.Key())
		}
	}
}

func TestOffersRankTheAuthorsGPUOrderBeforeACheaperCard(t *testing.T) {
	in := offerInputs(t)
	gpuType := func(name, model string, micros int64) CatalogType {
		return CatalogType{Name: name, CPUMillis: 16_000, MemoryBytes: 64 * gib, GPU: model, GPUCount: 1, prices: [4]int64{micros, micros, micros, micros}}
	}
	in.Catalog = []CatalogType{gpuType("l40s", "L40S", 1_000_000), gpuType("l4", "L4", 300_000), gpuType("t4", "T4", 200_000), mustType(t, "m7i.large")}
	preferred := RankOffers(DefaultPolicy(), Requirement{GPUs: []string{"L40S", "L4"}, CPUMillis: 1000, MemoryBytes: gib}, false, in)
	if len(preferred) == 0 || preferred[0].Type.GPU != "L40S" {
		t.Fatalf("first offer %v", offerKeys(preferred))
	}
	anyCard := RankOffers(DefaultPolicy(), Requirement{GPUs: []string{GPUAny}, CPUMillis: 1000, MemoryBytes: gib}, false, in)
	if len(anyCard) == 0 || anyCard[0].Type.Name != "t4" {
		t.Fatalf("cheapest card first: %v", offerKeys(anyCard))
	}
	for _, o := range RankOffers(DefaultPolicy(), Requirement{CPUMillis: 1000, MemoryBytes: gib}, false, in) {
		if o.Type.GPUCount > 0 {
			t.Fatalf("CPU work offered GPU host %s", o.Key())
		}
	}
}

// The fleet buys only GPU models billing has enabled, even for work that
// names a disabled one first.
func TestOffersHoldOnlyEnabledGPUModels(t *testing.T) {
	in := offerInputs(t)
	in.Networks = map[string]Network{"us-east-1": oneZone("us-east-1a", "use1-az1")}
	offers := RankOffers(DefaultPolicy(), Requirement{GPUs: []string{"H100", GPUAny}, CPUMillis: 1000, MemoryBytes: gib}, false, in)
	if len(offers) == 0 {
		t.Fatal("no offer for any model")
	}
	for _, o := range offers {
		if !billing.GPUEnabled(o.Type.GPU) {
			t.Fatalf("offered %s", o.Key())
		}
	}
}

func TestOfferCostIsComputeRootDiskAndPublicIPv4(t *testing.T) {
	in := offerInputs(t)
	in.Networks = map[string]Network{"us-west-1": oneZone("us-west-1a", "usw1-az1")}
	in.Catalog = []CatalogType{mustType(t, "m7i.large")}
	serving := RankOffers(DefaultPolicy(), Requirement{}, false, in)
	reserve := RankOffers(DefaultPolicy(), Requirement{}, true, in)
	if len(serving) != 1 || len(reserve) != 1 {
		t.Fatalf("offers %v %v", offerKeys(serving), offerKeys(reserve))
	}
	// 100 GiB of gp3 at $0.096 a GiB-month in us-west-1 over 720 hours.
	disk := (100*96_000 + 719) / 720
	if o := serving[0]; o.HourlyMicros != 117_600+int64(disk)+5_000 || o.StoppedMicros != int64(disk) || o.Hibernate {
		t.Fatalf("serving offer %+v", o)
	}
	// A hibernating reserve adds its 8 GiB of RAM as swap.
	disk = (108*96_000 + 719) / 720
	if o := reserve[0]; !o.Hibernate || o.StoppedMicros != int64(disk) || o.HourlyMicros != 117_600+int64(disk)+5_000 {
		t.Fatalf("reserve offer %+v", o)
	}
}

func TestPurchaseMarginKeepsThirtyPercentOfRateCardRevenue(t *testing.T) {
	p := DefaultPolicy()
	rates := indexRates(fleetRates(t))
	usable := mustType(t, "m6a.8xlarge").Usable(0)
	revenue := func(class billing.RateClass, gpu billing.GPUType, cards int) int64 {
		r := rates[class][gpu]
		return usable.CPUMillis*r.CPUCoreHour/1000 + usable.MemoryBytes/gib*r.MemoryGiBHour +
			usable.MemoryBytes%gib*r.MemoryGiBHour/gib + int64(cards)*r.GPUCardHour
	}
	ceiling := func(revenue int64) int64 { return revenue * 70 / 100_000 }
	cases := []struct {
		name        string
		market      Market
		preemptible bool
		gpu         string
		cards       int
		class       billing.RateClass
	}{
		{"Spot", MarketSpot, true, "", 0, billing.ClassAuto},
		{"on-demand", MarketOnDemand, false, "", 0, billing.ClassNonPreemptible},
		{"Spot-tolerant work on on-demand keeps its lower rate", MarketOnDemand, true, "", 0, billing.ClassAuto},
		{"on-demand GPU", MarketOnDemand, false, "L4", 1, billing.ClassNonPreemptible},
	}
	for _, c := range cases {
		limit := ceiling(revenue(c.class, billing.GPUType(c.gpu), c.cards))
		o := FleetOffer{Type: CatalogType{GPU: c.gpu, GPUCount: c.cards}, Market: c.market, Usable: usable}
		o.Usable.GPUs = c.cards
		o.HourlyMicros = limit
		if reason, rejected := marginRejection(p, rates, o, c.preemptible); rejected {
			t.Errorf("%s at its ceiling %d: %s", c.name, limit, reason)
		}
		o.HourlyMicros = limit + 1
		if reason, _ := marginRejection(p, rates, o, c.preemptible); reason != rejectInsufficientMargin {
			t.Errorf("%s over its ceiling: %q", c.name, reason)
		}
	}
	nonPreemptible := rates[billing.ClassNonPreemptible][""]
	auto := rates[billing.ClassAuto][""]
	if nonPreemptible.CPUCoreHour != 3*auto.CPUCoreHour || rates[billing.ClassNonPreemptible]["L4"].GPUCardHour != rates[billing.ClassAuto]["L4"].GPUCardHour {
		t.Fatalf("non-preemptible CPU and memory are three times automatic; GPUs are not")
	}
	unpriced := FleetOffer{Type: CatalogType{GPU: "unpriced", GPUCount: 1}, Market: MarketOnDemand, Usable: usable, HourlyMicros: 1}
	if reason, _ := marginRejection(p, rates, unpriced, false); reason != rejectUnpricedCapacity {
		t.Fatalf("unpriced GPU: %q", reason)
	}
}

func TestRefusalsCoolTheOfferAndTwoInARegionMoveBuyingToTheNext(t *testing.T) {
	in := offerInputs(t)
	in.Catalog = []CatalogType{mustType(t, "m7i.large"), mustType(t, "m7i.xlarge")}
	in.Networks["us-east-1"] = oneZone("us-east-1a", "use1-az1")
	need := Requirement{CPUMillis: 1000, MemoryBytes: gib}
	refused := func(instanceType string, at time.Time) OfferCooldown {
		return OfferCooldown{Region: "us-east-2", InstanceType: instanceType, Market: MarketOnDemand, RefusedAt: at, Until: at.Add(10 * time.Minute)}
	}
	in.Cooldowns = []OfferCooldown{refused("m7i.large", offerNow.Add(-time.Minute))}
	offers := RankOffers(DefaultPolicy(), need, false, in)
	keys := offerKeys(offers)
	if slices.Contains(keys, "us-east-2/use2-az1/m7i.large/on_demand") || !slices.Contains(keys, "us-east-2/use2-az1/m7i.xlarge/on_demand") {
		t.Fatalf("one refusal cools only its offer: %v", keys)
	}
	in.Cooldowns = append(in.Cooldowns, refused("m7i.xlarge", offerNow.Add(-20*time.Minute)))
	offers = RankOffers(DefaultPolicy(), need, false, in)
	if last := offers[len(offers)-1]; !last.CoolingRegion || offers[0].CoolingRegion {
		t.Fatalf("a cooling region ranks last: %v", offerKeys(offers))
	}
	for _, o := range preferHealthy(offers) {
		if o.Region == "us-east-2" {
			t.Fatalf("a cooling region is passed over while another serves: %v", offerKeys(offers))
		}
	}
	delete(in.Networks, "us-east-1")
	in.Cooldowns[1].Until = offerNow.Add(-time.Minute)
	offers = RankOffers(DefaultPolicy(), need, false, in)
	if len(offers) != 1 || !offers[0].CoolingRegion {
		t.Fatalf("a cooling region still serves when nothing else does: %v", offerKeys(offers))
	}
	in.Cooldowns[1].RefusedAt = offerNow.Add(-31 * time.Minute)
	if offers = RankOffers(DefaultPolicy(), need, false, in); offers[0].CoolingRegion {
		t.Fatalf("refusals older than the window still cool the region")
	}
}

func TestOffersPreferTheRegionOrderThenTheEmptierZone(t *testing.T) {
	in := offerInputs(t)
	in.Catalog = []CatalogType{mustType(t, "m7i.large")}
	in.Networks = map[string]Network{
		"us-east-1": oneZone("us-east-1a", "use1-az1"),
		"us-east-2": {Subnets: []Subnet{{ID: "a", Zone: "us-east-2a", ZoneID: "use2-az1"}, {ID: "b", Zone: "us-east-2b", ZoneID: "use2-az2"}}},
	}
	in.ZoneHosts = map[string]int{"use2-az1": 3, "use2-az2": 1}
	offers := RankOffers(DefaultPolicy(), Requirement{}, false, in)
	want := []string{"us-east-2/use2-az2/m7i.large/on_demand", "us-east-2/use2-az1/m7i.large/on_demand", "us-east-1/use1-az1/m7i.large/on_demand"}
	if got := offerKeys(offers); len(got) != 3 || got[0] != want[0] || got[1] != want[1] || got[2] != want[2] {
		t.Fatalf("order %v", got)
	}
}

func mustType(t *testing.T, name string) CatalogType {
	t.Helper()
	typ, ok := CatalogTypeNamed(name)
	if !ok {
		t.Fatalf("no catalog type %s", name)
	}
	return typ
}

func offerKeys(offers []FleetOffer) []string {
	keys := make([]string, len(offers))
	for i, o := range offers {
		keys[i] = o.Key()
	}
	return keys
}

func TestAConnectionPaysItsOwnHostsAndOnlyOnDemandReservesHibernate(t *testing.T) {
	in := offerInputs(t)
	in.Rates = nil
	need := Requirement{CPUMillis: 1000, MemoryBytes: gib}
	if offers := RankOffers(DefaultPolicy(), need, false, in); len(offers) != 0 {
		t.Fatalf("the platform bought %d offers no rate prices", len(offers))
	}
	in.OwnerPays = true
	if offers := RankOffers(DefaultPolicy(), need, false, in); len(offers) == 0 {
		t.Fatal("a connected account got no offers")
	}
	in = offerInputs(t)
	in.Catalog = []CatalogType{mustType(t, "m7i.xlarge")}
	in.Spot = []SpotQuote{{Region: "us-east-2", ZoneID: "use2-az1", InstanceType: "m7i.xlarge", HourlyMicros: 80_000, ObservedAt: offerNow}}
	for _, preemptible := range []bool{false, true} {
		need.Preemptible = preemptible
		offers := RankOffers(DefaultPolicy(), need, true, in)
		if len(offers) == 0 || offers[0].Hibernate == preemptible ||
			offers[0].StoppedMicros != rootDiskMicros("us-east-2", offers[0].Type.RootGiB(offers[0].Hibernate)) {
			t.Errorf("preemptible %v: reserve offers %v, want the %s one to hibernate %v", preemptible, offerKeys(offers),
				map[bool]string{true: "Spot", false: "on-demand"}[preemptible], !preemptible)
		}
	}
}

// An on-demand reserve hibernates only with at most 32 GiB of RAM; a larger
// image would not finish writing within providerDeadline, so it stops
// plainly on the plain root.
func TestOnlyReservesOfAtMost32GiBHibernate(t *testing.T) {
	in := offerInputs(t)
	for name, want := range map[string]bool{"m7i.2xlarge": true, "m6a.4xlarge": false, "r6a.4xlarge": false, "m7i.8xlarge": false} {
		typ := mustType(t, name)
		in.Catalog = []CatalogType{typ}
		offers := RankOffers(DefaultPolicy(), Requirement{CPUMillis: 1000, MemoryBytes: gib}, true, in)
		if len(offers) == 0 || offers[0].Hibernate != want || offers[0].StoppedMicros != rootDiskMicros("us-east-2", typ.RootGiB(want)) {
			t.Errorf("%s: reserve offers %v, want one that hibernates %v", name, offerKeys(offers), want)
		}
	}
}

func TestOffersSkipAZoneThatDoesNotOfferTheType(t *testing.T) {
	in := offerInputs(t)
	in.Catalog = []CatalogType{mustType(t, "m7i.large"), mustType(t, "g4dn.xlarge")}
	in.Networks = map[string]Network{
		"us-east-1": {Subnets: []Subnet{{ID: "a", Zone: "us-east-1e", ZoneID: "use1-az3"}, {ID: "b", Zone: "us-east-1a", ZoneID: "use1-az6"}}},
		"us-east-2": oneZone("us-east-2a", "use2-az1"),
	}
	// us-east-1 was read and use1-az3 offers neither type; us-east-2 was
	// not read, so nothing limits it.
	in.ZoneTypes = map[string]map[string][]string{"us-east-1": {"use1-az6": {"g4dn.xlarge", "m7i.large"}}}
	got := offerKeys(RankOffers(DefaultPolicy(), Requirement{}, false, in))
	want := []string{"us-east-2/use2-az1/m7i.large/on_demand", "us-east-1/use1-az6/m7i.large/on_demand"}
	if !slices.Equal(got, want) {
		t.Fatalf("offers %v, want %v", got, want)
	}
}

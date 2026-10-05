package compute

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestOffersSkipTypesAKnownVCPUQuotaCannotHold(t *testing.T) {
	in := offerInputs(t)
	in.Networks = map[string]Network{"us-west-2": oneZone("us-west-2a", "usw2-az1")}
	in.Spot = []SpotQuote{
		{Region: "us-west-2", ZoneID: "usw2-az1", InstanceType: "g4dn.xlarge", HourlyMicros: 200_000, ObservedAt: offerNow},
		{Region: "us-west-2", ZoneID: "usw2-az1", InstanceType: "m7i.xlarge", HourlyMicros: 70_000, ObservedAt: offerNow},
	}
	in.Catalog = []CatalogType{mustType(t, "g4dn.xlarge"), mustType(t, "g4dn.2xlarge"), mustType(t, "m7i.xlarge")}
	need := Requirement{Preemptible: true, GPUs: []string{"T4"}, CPUMillis: 1000, MemoryBytes: gib}
	if offers := RankOffers(DefaultPolicy(), need, false, in); len(offers) == 0 || offers[0].Market != MarketSpot {
		t.Fatalf("without a quota: %v", offerKeys(offers))
	}
	in.Quotas = []VCPUQuota{{Key: QuotaKey{Region: "us-west-2", Class: QuotaG, Market: MarketSpot}, VCPUs: 0}}
	for _, o := range RankOffers(DefaultPolicy(), need, false, in) {
		if o.Market == MarketSpot {
			t.Fatalf("Oregon's G Spot quota is 0, yet %s is offered", o.Key())
		}
	}
	in.Quotas[0].VCPUs = 8
	in.QuotaUsed = map[QuotaKey]int64{in.Quotas[0].Key: 4}
	offers := RankOffers(DefaultPolicy(), need, false, in)
	if len(offers) != 1 || offers[0].Type.Name != "g4dn.xlarge" {
		t.Fatalf("4 vCPUs left take only the 4 vCPU type: %v", offerKeys(offers))
	}
	if cpu := RankOffers(DefaultPolicy(), Requirement{Preemptible: true}, false, in); len(cpu) == 0 || cpu[0].Market != MarketSpot {
		t.Fatalf("the G quota does not hold standard types back: %v", offerKeys(cpu))
	}
}

func TestAQuotaRefusalCoolsTheWholeClassInItsRegionAndMarket(t *testing.T) {
	in := offerInputs(t)
	in.Catalog = []CatalogType{mustType(t, "m7i.large"), mustType(t, "c6a.2xlarge"), mustType(t, "g4dn.2xlarge")}
	in.Cooldowns = []OfferCooldown{{
		Region: "us-east-2", InstanceType: "m7i.large", Market: MarketOnDemand, RefusedAt: offerNow, Until: offerNow.Add(10 * time.Minute), Quota: true,
	}}
	if offers := RankOffers(DefaultPolicy(), Requirement{}, false, in); len(offers) != 0 {
		t.Fatalf("standard types still offered: %v", offerKeys(offers))
	}
	if offers := RankOffers(DefaultPolicy(), Requirement{GPUs: []string{"T4"}}, false, in); len(offers) != 1 {
		t.Fatalf("a standard refusal cooled G types: %v", offerKeys(offers))
	}
}

func TestCoverStaysWithinTheQuotaRoomAcrossNodes(t *testing.T) {
	key := QuotaKey{Region: "us-east-2", Class: QuotaStandard, Market: MarketOnDemand}
	eight := mustType(t, "c6a.2xlarge")
	o := FleetOffer{Type: eight, Usable: eight.Usable(0), HourlyMicros: 306_000, Quota: key}
	need := CoverNeed{Aggregate: eight.Usable(0).Times(3)}
	if r := Cover([]FleetOffer{o}, need, hourlyCost, CoverLimits{Nodes: 16}); len(r.Nodes) != 3 {
		t.Fatalf("unlimited: %d nodes", len(r.Nodes))
	}
	r := Cover([]FleetOffer{o}, need, hourlyCost, CoverLimits{Nodes: 16, VCPUs: map[QuotaKey]int64{key: 16}})
	if len(r.Nodes) != 2 || r.Complete(need) {
		t.Fatalf("16 vCPUs of room: %d nodes", len(r.Nodes))
	}
}

func TestPlanCountsRunningHostsAgainstQuotasButNotStoppedReserves(t *testing.T) {
	key := QuotaKey{Region: "us-east-2", Class: QuotaStandard, Market: MarketOnDemand}
	s := planSnapshot(t, planHost(1, planSmall, FleetServing), planHost(2, planSmall, FleetStopped))
	s.Offers.Quotas = []VCPUQuota{{Key: key, VCPUs: 48}}
	s.Hosts[0].Load = small
	plan := PlanFleet(planPolicy(small.Times(3), FleetCapacity{}), s)
	// 16 vCPUs run; the stopped reserve resumes into 16 more and one host
	// is bought with the last 16.
	if len(actionsOf(plan, ActionResume)) != 1 || len(actionsOf(plan, ActionBuy)) != 1 {
		t.Fatalf("actions %+v", plan.Actions)
	}
	g, _ := pendingOne(Requirement{CPUMillis: 1000, MemoryBytes: gib}, nil)
	s.Pending = []DemandGroup{g, {Need: g.Need, Containers: []PendingContainer{{ID: uuid.New()}}}}
	s.Offers.Quotas[0].VCPUs = 16
	s.Hosts = s.Hosts[:1]
	if plan := PlanFleet(planPolicy(FleetCapacity{}, FleetCapacity{}), s); len(plan.Actions) > 0 {
		t.Fatalf("a full quota still buys: %+v", plan.Actions)
	}
}

package compute

import (
	"slices"
	"testing"
)

// coverOffer is an offer of a usable shape at an hourly price.
func coverOffer(name string, usable FleetCapacity, hourly int64) FleetOffer {
	return FleetOffer{Type: CatalogType{Name: name}, Region: "us-east-2", ZoneID: "use2-az1", Market: MarketOnDemand, Usable: usable, HourlyMicros: hourly, StoppedMicros: hourly / 10}
}

var (
	coverSmall = coverOffer("small", cpuGiB(8000, 16), 100_000)
	coverLarge = coverOffer("large", cpuGiB(32_000, 64), 300_000)
)

func hourlyCost(o FleetOffer) int64 { return o.HourlyMicros }

func boughtNames(r CoverResult) []string {
	var names []string
	for _, n := range r.Nodes {
		names = append(names, n.Offer.Type.Name)
	}
	slices.Sort(names)
	return names
}

func TestCoverChoosesTheLowerTotalCostForRequiredCapacity(t *testing.T) {
	for _, c := range []struct {
		need FleetCapacity
		want []string
	}{
		{coverSmall.Usable, []string{"small"}},
		{coverSmall.Usable.Times(4), []string{"large"}},
	} {
		need := CoverNeed{Aggregate: c.need}
		r := Cover([]FleetOffer{coverSmall, coverLarge}, need, hourlyCost, CoverLimits{Nodes: 16})
		if !slices.Equal(boughtNames(r), c.want) || !r.Complete(need) {
			t.Errorf("aggregate %+v bought %v", c.need, boughtNames(r))
		}
	}
}

func TestCoverFitsALargeRequestOnOneHostDespiteAggregateRoom(t *testing.T) {
	items := CoverNeed{Items: []CoverItem{{Shape: cpuGiB(16_000, 32), Count: 1}}}
	if r := Cover([]FleetOffer{coverSmall, coverLarge}, items, hourlyCost, CoverLimits{Nodes: 16}); !slices.Equal(boughtNames(r), []string{"large"}) {
		t.Fatalf("items bought %v", boughtNames(r))
	}
}

func TestCoverCountsEachContainerFittingANode(t *testing.T) {
	medium := coverOffer("medium", cpuGiB(16_000, 32), 250_000)
	need := CoverNeed{Items: []CoverItem{{Shape: cpuGiB(5000, 10), Count: 3}}}
	r := Cover([]FleetOffer{coverSmall, medium}, need, hourlyCost, CoverLimits{Nodes: 16})
	if len(r.Nodes) != 1 || r.Nodes[0].Offer.Type.Name != "medium" || r.Nodes[0].Placed[0] != 3 || len(r.UnmetItems) != 0 {
		t.Fatalf("cover %+v", r)
	}
}

func TestCoverReportsWhatNoOfferCanPlace(t *testing.T) {
	request := cpuGiB(5000, 10)
	need := CoverNeed{Items: []CoverItem{{Shape: request, Count: 2}, {Shape: coverLarge.Usable, Count: 1}}}
	r := Cover([]FleetOffer{coverSmall}, need, hourlyCost, CoverLimits{Nodes: 16})
	if len(r.Nodes) != 2 || len(r.UnmetItems) != 1 || r.UnmetItems[0] != (CoverItem{Shape: coverLarge.Usable, Count: 1}) {
		t.Fatalf("cover %+v", r)
	}
	placed := 0
	for _, n := range r.Nodes {
		placed += n.Placed[0] + n.Placed[1]
	}
	if placed != 2 {
		t.Fatalf("placed %d", placed)
	}
}

func TestCoverStopsAtItsNodeBoundAndReturnsTheBestPartial(t *testing.T) {
	need := CoverNeed{Items: []CoverItem{{Shape: cpuGiB(7000, 14), Count: 5}}}
	r := Cover([]FleetOffer{coverSmall}, need, hourlyCost, CoverLimits{Nodes: 3})
	if len(r.Nodes) != 3 || r.UnmetItems[0].Count != 2 {
		t.Fatalf("cover %+v", r)
	}
	if r := Cover([]FleetOffer{coverSmall}, need, hourlyCost, CoverLimits{Nodes: 0}); len(r.Nodes) != 0 || r.UnmetItems[0].Count != 5 {
		t.Fatalf("no room still reports the need: %+v", r)
	}
}

func TestCoverPacksTwoSixCPURequestsOntoOneLargerHost(t *testing.T) {
	// An 8 vCPU host offers 7.2 vCPU, so a 6 vCPU request takes one alone;
	// two share a 16 vCPU host, which costs no more and keeps one host.
	eight := mustType(t, "c6a.2xlarge")
	sixteen := mustType(t, "c6a.4xlarge")
	offers := []FleetOffer{
		{Type: eight, Usable: eight.Usable(0), HourlyMicros: 306_000},
		{Type: sixteen, Usable: sixteen.Usable(0), HourlyMicros: 612_000},
	}
	for _, c := range []struct {
		count int
		want  []string
	}{
		{1, []string{"c6a.2xlarge"}},
		{2, []string{"c6a.4xlarge"}},
	} {
		need := CoverNeed{Items: []CoverItem{{Shape: cpuGiB(6000, 4), Count: c.count}}}
		if r := Cover(offers, need, hourlyCost, CoverLimits{Nodes: 16}); !slices.Equal(boughtNames(r), c.want) {
			t.Errorf("%d requests bought %v", c.count, boughtNames(r))
		}
	}
}

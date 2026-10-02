package compute_test

import (
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// filterValues are the values of a Query API call's named filter.
func filterValues(call awsCall, name string) []string {
	for n := 1; ; n++ {
		key := fmt.Sprintf("Filter.%d.", n)
		switch call.Form.Get(key + "Name") {
		case "":
			return nil
		case name:
			var values []string
			for v := 1; call.Form.Has(fmt.Sprintf("%sValue.%d", key, v)); v++ {
				values = append(values, call.Form.Get(fmt.Sprintf("%sValue.%d", key, v)))
			}
			return values
		}
	}
}

// A zone that does not offer a type gets no purchase of it: EC2 would
// refuse the launch, and the refusal would cool the type in every zone of
// the region.
func TestPurchasesSkipAZoneThatDoesNotOfferTheType(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	run(t, o.pool, `insert into fleet_zone_offerings values ('us-west-1', 'usw1-az3', 'm7i.large', now() - interval '2 hours')`)
	emulator.on("DescribeInstanceTypeOfferings", func(call awsCall) awsReply {
		zones := filterValues(call, "location")
		if call.Form.Get("LocationType") != "availability-zone-id" || len(zones) == 0 || strings.HasPrefix(zones[0], "usw1") {
			return ec2Error(http.StatusServiceUnavailable, "Unavailable", "The service is unavailable")
		}
		// us-east-2a (use2-az1) offers no catalog type.
		var b strings.Builder
		b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<DescribeInstanceTypeOfferingsResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><instanceTypeOfferingSet>`)
		for _, typ := range filterValues(call, "instance-type") {
			fmt.Fprintf(&b, `<item><instanceType>%s</instanceType><locationType>availability-zone-id</locationType><location>use2-az2</location></item>`, esc(typ))
		}
		b.WriteString(`</instanceTypeOfferingSet></DescribeInstanceTypeOfferingsResponse>`)
		return ok(b.String())
	})
	stored, err := o.compute.RefreshZoneOfferings(t.Context(), discard())
	if stored == 0 || err == nil || !strings.Contains(err.Error(), "us-west-1") {
		t.Fatalf("refresh stored %d (%v), want us-east-2's offerings and the us-west-1 failure", stored, err)
	}
	if n := scan[int](t, o.pool, "select count(*)::int from fleet_zone_offerings where region = 'us-west-1'"); n != 1 {
		t.Fatalf("us-west-1 holds %d offerings after its read failed, want its last one", n)
	}

	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"placement": {"preemptible": false, "region": "us-east"}}`), 1000, gib)
	if result := planCapacity(t, o); result.Requested != 1 {
		t.Fatalf("result %+v, want one host", result)
	}
	if got := requested(t, o); len(got) != 1 || !strings.Contains(got[0], "us-east-2b") {
		t.Fatalf("requested %q, want a host in us-east-2b, the zone that offers it", got)
	}
}

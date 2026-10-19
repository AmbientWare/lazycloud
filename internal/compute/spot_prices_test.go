package compute_test

import (
	"fmt"
	"maps"
	"net/http"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

func spotPriceReply(prices ...[4]string) awsReply {
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<DescribeSpotPriceHistoryResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><spotPriceHistorySet>`)
	for _, p := range prices {
		fmt.Fprintf(&b, `<item><instanceType>%s</instanceType><productDescription>Linux/UNIX</productDescription>`+
			`<spotPrice>%s</spotPrice><timestamp>%s</timestamp><availabilityZoneId>%s</availabilityZoneId></item>`,
			esc(p[0]), esc(p[1]), esc(p[2]), esc(p[3]))
	}
	b.WriteString(`</spotPriceHistorySet><nextToken/></DescribeSpotPriceHistoryResponse>`)
	return ok(b.String())
}

// placementScoreReply answers a placement score read with scores by zone
// id, each in the region its id names. Recorded from GetSpotPlacementScores
// in account 534742592531 on 2026-10-19 (g5.2xlarge, the four US regions,
// one instance in a single zone): ten zones, best first, and no NextToken,
// also with MaxResults 100.
func placementScoreReply(scores map[string]int) awsReply {
	regions := map[string]string{"use1": "us-east-1", "use2": "us-east-2", "usw1": "us-west-1", "usw2": "us-west-2"}
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<GetSpotPlacementScoresResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><requestId>req-0000</requestId><spotPlacementScoreSet>`)
	for _, zone := range slices.Sorted(maps.Keys(scores)) {
		prefix, _, _ := strings.Cut(zone, "-")
		fmt.Fprintf(&b, `<item><region>%s</region><availabilityZoneId>%s</availabilityZoneId><score>%d</score></item>`,
			esc(regions[prefix]), esc(zone), scores[zone])
	}
	b.WriteString(`</spotPlacementScoreSet></GetSpotPlacementScoresResponse>`)
	return ok(b.String())
}

// The planner buys a pending container's Spot host in the pool that wins
// on price and placement score, and never in a pool AWS scores 1: an
// 8-vCPU pool scored 5 against a best of 9 loses to one 10% dearer and wins
// against one 30% dearer; scored 1, or left out of a full answer whose
// lowest is 1, it loses to one 50% dearer scored 2. Each read names every
// fleet region and one instance of a GPU type alone or of a shape of three
// types or more, as AWS scores them.
func TestPlacementScoresRankAScarcePoolBelowASlightlyDearerOne(t *testing.T) {
	t.Parallel()
	// AWS answers with at most ten zones, the best scored.
	full := map[string]int{"use2-az2": 2, "use2-az3": 1}
	for _, zone := range []string{"usw1-az1", "usw1-az3", "use2-az4", "use2-az5", "use2-az6", "use2-az7", "use2-az8", "use2-az9"} {
		full[zone] = 1
	}
	for _, c := range []struct {
		name, dearPrice string
		scores          map[string]int
		want            string
	}{
		{"scored 5, 10% dearer", "0.110000", map[string]int{"use2-az1": 5, "use2-az2": 9}, "use2-az2"},
		{"scored 5, 30% dearer", "0.130000", map[string]int{"use2-az1": 5, "use2-az2": 9}, "use2-az1"},
		{"scored 1", "0.150000", map[string]int{"use2-az1": 1, "use2-az2": 2}, "use2-az2"},
		{"left out", "0.150000", full, "use2-az2"},
	} {
		o, emulator, _ := launchFleet(t, compute.Fleet{})
		prices := map[string]string{"use2-az1": "0.100000", "use2-az2": c.dearPrice}
		emulator.on("DescribeSpotPriceHistory", func(call awsCall) awsReply {
			zone := call.Form.Get("AvailabilityZoneId")
			if price, ok := prices[zone]; ok {
				return spotPriceReply([4]string{"c6a.2xlarge", price, "2026-10-07T12:00:00Z", zone})
			}
			return spotPriceReply()
		})
		emulator.on("GetSpotPlacementScores", func(call awsCall) awsReply {
			types := list(call.Form, "InstanceType")
			gpu := len(types) == 1 && (types[0][0] == 'g' || types[0][0] == 'p')
			if call.Form.Get("SingleAvailabilityZone") != "true" || call.Form.Get("TargetCapacity") != "1" ||
				!slices.Equal(list(call.Form, "RegionName"), []string{"us-east-2", "us-west-1"}) || (len(types) < 3 && !gpu) {
				t.Errorf("GetSpotPlacementScores %v, want one instance of a GPU type or a shape of three types or more in every fleet region", call.Form)
			}
			if slices.Contains(types, "c6a.2xlarge") {
				return placementScoreReply(c.scores)
			}
			return placementScoreReply(nil)
		})
		if _, err := o.compute.RefreshSpotPrices(t.Context(), discard()); err != nil {
			t.Fatal(err)
		}
		alice := newUser(t, o.pool, "alice@example.com")
		dev := newWorkspace(t, o.pool, "dev", alice)
		container := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{}`), 4000, 8*gib)
		plan(t, o)
		got := scan[string](t, o.pool, `
select h.instance_type || ' ' || h.market || ' ' || h.availability_zone_id
from hosts h join containers c on c.capacity_host_id = h.id where c.id = $1`, container)
		if want := "c6a.2xlarge spot " + c.want; got != want {
			t.Errorf("%s: bought %s, want %s", c.name, got, want)
		}
	}
}

func TestSpotPricesKeepTheLatestQuotePerZoneAndAFailedRegionKeepsItsPrices(t *testing.T) {
	t.Parallel()
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	run(t, o.pool, `insert into spot_prices values ('us-west-1', 'usw1-az3', 'm7i.large', 40000, now() - interval '20 minutes', now() - interval '20 minutes')`)
	emulator.on("DescribeSpotPriceHistory", func(call awsCall) awsReply {
		if call.Form.Get("StartTime") == "" || call.Form.Get("ProductDescription.1") != "Linux/UNIX" {
			t.Errorf("DescribeSpotPriceHistory %v, want the current Linux price", call.Form)
		}
		switch zone := call.Form.Get("AvailabilityZoneId"); zone {
		case "use2-az1":
			return spotPriceReply(
				[4]string{"m7i.large", "0.031000", "2026-10-02T10:00:00Z", zone},
				[4]string{"m7i.large", "0.0331001", "2026-10-02T11:00:00Z", zone},
				[4]string{"m7i.xlarge", "0.066200", "2026-10-02T09:00:00Z", zone},
			)
		case "use2-az2":
			return spotPriceReply([4]string{"m7i.large", "0.029800", "2026-10-02T11:30:00Z", zone})
		}
		return ec2Error(http.StatusServiceUnavailable, "Unavailable", "The service is unavailable")
	})
	emulator.on("GetSpotPlacementScores", func(awsCall) awsReply { return placementScoreReply(nil) })

	stored, err := o.compute.RefreshSpotPrices(t.Context(), discard())
	if stored != 3 || err == nil || !strings.Contains(err.Error(), "us-west-1") {
		t.Fatalf("refresh stored %d (%v), want three us-east-2 prices and the us-west-1 failure", stored, err)
	}
	prices, err := o.compute.SpotPrices(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]int64{}
	for _, p := range prices {
		got[p.ZoneID+"/"+p.InstanceType] = p.HourlyMicros
	}
	want := map[string]int64{
		// The latest quote wins, and a fraction past micros rounds up.
		"use2-az1/m7i.large": 33101, "use2-az1/m7i.xlarge": 66200, "use2-az2/m7i.large": 29800,
		"usw1-az3/m7i.large": 40000,
	}
	if len(got) != len(want) {
		t.Fatalf("prices %v, want %v", got, want)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("price %s = %d, want %d", k, got[k], v)
		}
	}
	run(t, o.pool, "update spot_prices set observed_at = now() - interval '2 hours' where region = 'us-west-1'")
	prices, err = o.compute.SpotPrices(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range prices {
		if p.Region == "us-west-1" || time.Since(p.ObservedAt) > time.Hour {
			t.Fatalf("price %+v observed over an hour ago is still offered", p)
		}
	}
}

// After the scheduler was down past the prices' age, the first pass
// refreshes every region's Spot prices before it buys: the L4 job buys a
// Spot g6.xlarge rather than waiting on prices it has none of.
func TestThePassAfterAnOutageRefreshesStaleSpotPricesFirst(t *testing.T) {
	t.Parallel()
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	spotPrices(t, o)
	run(t, o.pool, "update spot_prices set observed_at = now() - interval '5 hours'")
	emulator.on("DescribeSpotPriceHistory", func(call awsCall) awsReply {
		return spotPriceReply([4]string{"g6.xlarge", "0.400000", time.Now().UTC().Format(time.RFC3339), call.Form.Get("AvailabilityZoneId")})
	})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	container := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"resources": {"gpu": ["L4"]}}`), 2000, 12*gib)
	plan(t, o)
	got := scan[*string](t, o.pool, `
select h.instance_type || ' ' || h.market from hosts h join containers c on c.capacity_host_id = h.id where c.id = $1`, container)
	if got == nil || *got != "g6.xlarge spot" {
		t.Fatalf("bought %v, want a Spot g6.xlarge priced by a refresh", got)
	}
}

// EC2 has the same capacity in a zone for every account: a pool another
// account was refused in cools for the platform too, while another
// account's quota refusal of a whole region does not.
func TestAZoneAnotherAccountWasRefusedInCoolsForEveryAccount(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name, zone, want string
	}{{"zone refused", "use2-az1", "use2-az2"}, {"region refused", "", "use2-az1"}} {
		o, emulator, _ := launchFleet(t, compute.Fleet{})
		prices := map[string]string{"use2-az1": "0.100000", "use2-az2": "0.110000"}
		emulator.on("DescribeSpotPriceHistory", func(call awsCall) awsReply {
			zone := call.Form.Get("AvailabilityZoneId")
			if price, ok := prices[zone]; ok {
				return spotPriceReply([4]string{"c6a.2xlarge", price, "2026-10-07T12:00:00Z", zone})
			}
			return spotPriceReply()
		})
		emulator.on("GetSpotPlacementScores", func(awsCall) awsReply { return placementScoreReply(nil) })
		if _, err := o.compute.RefreshSpotPrices(t.Context(), discard()); err != nil {
			t.Fatal(err)
		}
		run(t, o.pool, `insert into capacity_cooldowns (connection_key, region, availability_zone_id, instance_type, market, until, cooled_for, reason)
values ($1, 'us-east-2', $2, 'c6a.2xlarge', 'spot', now() + interval '10 minutes', interval '10 minutes', 'refused')`, uuid.NewString(), c.zone)
		alice := newUser(t, o.pool, "alice@example.com")
		dev := newWorkspace(t, o.pool, "dev", alice)
		container := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{}`), 4000, 8*gib)
		plan(t, o)
		got := scan[string](t, o.pool, `
select h.instance_type || ' ' || h.market || ' ' || h.availability_zone_id
from hosts h join containers c on c.capacity_host_id = h.id where c.id = $1`, container)
		if want := "c6a.2xlarge spot " + c.want; got != want {
			t.Errorf("%s: bought %s, want %s", c.name, got, want)
		}
	}
}

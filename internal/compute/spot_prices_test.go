package compute_test

import (
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"

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

func TestSpotPricesKeepTheLatestQuotePerZoneAndAFailedRegionKeepsItsPrices(t *testing.T) {
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

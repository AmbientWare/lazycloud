package compute_test

import (
	"encoding/json"
	"os"
	"slices"
	"strconv"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// TestRealEC2SellsTheCatalogWhereItIsPriced checks, read-only, that EC2
// sells each catalog type in exactly the regions the catalog prices it in,
// that the hibernation flags match HibernationSupported, and that Spot
// prices exist. With LAZYCLOUD_FLEET_SPOT_SNAPSHOT naming a file it writes
// the latest Spot quote per type and zone there. It runs only with
// LAZYCLOUD_EC2_ACCEPTANCE_PROFILE naming a disposable profile and launches
// nothing.
func TestRealEC2SellsTheCatalogWhereItIsPriced(t *testing.T) {
	profile := os.Getenv("LAZYCLOUD_EC2_ACCEPTANCE_PROFILE")
	if profile == "" {
		t.Skip("set LAZYCLOUD_EC2_ACCEPTANCE_PROFILE to a disposable AWS profile")
	}
	var names []string
	for _, typ := range compute.FleetCatalog() {
		names = append(names, typ.Name)
	}
	type quote struct {
		Region       string    `json:"region"`
		ZoneID       string    `json:"zone_id"`
		InstanceType string    `json:"instance_type"`
		HourlyMicros int64     `json:"hourly_micros"`
		ObservedAt   time.Time `json:"observed_at"`
	}
	var quotes []quote
	for _, region := range []string{"us-east-2", "us-west-1", "us-east-1", "us-west-2"} {
		cfg, err := config.LoadDefaultConfig(t.Context(), config.WithSharedConfigProfile(profile), config.WithRegion(region))
		if err != nil {
			t.Fatal(err)
		}
		client := ec2.NewFromConfig(cfg)
		sold := map[string]bool{}
		offerings := ec2.NewDescribeInstanceTypeOfferingsPaginator(client, &ec2.DescribeInstanceTypeOfferingsInput{
			LocationType: ec2types.LocationTypeRegion,
			Filters:      []ec2types.Filter{{Name: aws.String("instance-type"), Values: names}},
		})
		for offerings.HasMorePages() {
			page, err := offerings.NextPage(t.Context())
			if err != nil {
				t.Fatalf("%s offerings: %v", region, err)
			}
			for _, o := range page.InstanceTypeOfferings {
				sold[string(o.InstanceType)] = true
			}
		}
		var hibernates []string
		types := ec2.NewDescribeInstanceTypesPaginator(client, &ec2.DescribeInstanceTypesInput{
			Filters: []ec2types.Filter{{Name: aws.String("hibernation-supported"), Values: []string{"true"}}, {Name: aws.String("instance-type"), Values: names}},
		})
		for types.HasMorePages() {
			page, err := types.NextPage(t.Context())
			if err != nil {
				t.Fatalf("%s instance types: %v", region, err)
			}
			for _, it := range page.InstanceTypes {
				hibernates = append(hibernates, string(it.InstanceType))
			}
		}
		var priced []string
		for _, typ := range compute.FleetCatalog() {
			_, ok := typ.OnDemandMicros(region)
			if ok != sold[typ.Name] {
				t.Errorf("%s in %s: priced %v, sold %v", typ.Name, region, ok, sold[typ.Name])
			}
			if ok {
				priced = append(priced, typ.Name)
			}
			if ok && typ.Hibernates && !slices.Contains(hibernates, typ.Name) {
				t.Errorf("%s does not hibernate in %s", typ.Name, region)
			}
		}
		var history []ec2types.SpotPrice
		spot := ec2.NewDescribeSpotPriceHistoryPaginator(client, &ec2.DescribeSpotPriceHistoryInput{
			StartTime: aws.Time(time.Now()), ProductDescriptions: []string{"Linux/UNIX"}, InstanceTypes: toTypes(priced),
		})
		for spot.HasMorePages() {
			page, err := spot.NextPage(t.Context())
			if err != nil {
				t.Fatalf("%s Spot prices: %v", region, err)
			}
			history = append(history, page.SpotPriceHistory...)
		}
		zones, err := client.DescribeAvailabilityZones(t.Context(), &ec2.DescribeAvailabilityZonesInput{})
		if err != nil {
			t.Fatal(err)
		}
		zoneIDs := map[string]string{}
		for _, z := range zones.AvailabilityZones {
			zoneIDs[aws.ToString(z.ZoneName)] = aws.ToString(z.ZoneId)
		}
		for _, p := range history {
			dollars, err := strconv.ParseFloat(aws.ToString(p.SpotPrice), 64)
			if err != nil {
				t.Fatal(err)
			}
			quotes = append(quotes, quote{
				Region: region, ZoneID: zoneIDs[aws.ToString(p.AvailabilityZone)], InstanceType: string(p.InstanceType),
				HourlyMicros: int64(dollars*1e6 + 0.5), ObservedAt: aws.ToTime(p.Timestamp),
			})
		}
		t.Logf("%s: %d of %d types sold, %d hibernate, %d Spot quotes", region, len(sold), len(names), len(hibernates), len(history))
	}
	if path := os.Getenv("LAZYCLOUD_FLEET_SPOT_SNAPSHOT"); path != "" {
		raw, err := json.MarshalIndent(map[string]any{"source": "DescribeSpotPriceHistory", "quotes": quotes}, "", " ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(raw, '\n'), 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

func toTypes(names []string) []ec2types.InstanceType {
	out := make([]ec2types.InstanceType, len(names))
	for i, n := range names {
		out[i] = ec2types.InstanceType(n)
	}
	return out
}

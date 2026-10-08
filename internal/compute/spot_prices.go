package compute

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"math"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"golang.org/x/sync/errgroup"
)

const (
	// SpotPriceInterval paces the Spot price refresh.
	SpotPriceInterval = 5 * time.Minute
	// spotPriceFresh is how long a Spot price stays good to buy on; a
	// region that fails to refresh keeps its prices until then.
	spotPriceFresh = time.Hour
	spotProduct    = "Linux/UNIX"
	// placementScoreFresh is how long a placement score ranks its pool: as
	// long as the price read with it. A region whose score read fails keeps
	// its scores until then.
	placementScoreFresh = spotPriceFresh
	// placementShapeTypes is the fewest catalog types a scored shape has,
	// the fewest AWS asks a placement score request to name.
	placementShapeTypes = 3
	// placementScoreCalls bounds a region's score reads in flight. AWS
	// refills 20 a second and charges nothing.
	placementScoreCalls = 4
)

// SpotQuote is the latest Spot price of a type in one availability zone.
type SpotQuote struct {
	Region       string
	ZoneID       string
	InstanceType string
	HourlyMicros int64
	EffectiveAt  time.Time
	ObservedAt   time.Time
	// PlacementScore is AWS's Spot placement score of the type's shape in
	// the zone, 1 to 10; 0 when no fresh score was read.
	PlacementScore int
}

// RefreshSpotPrices reads the current Spot price of every catalog type in
// every zone of the platform's networks and stores the latest per zone and
// type, one region per statement, then each priced pool's placement score.
// A region whose read fails keeps its last prices or scores. It reports how
// many prices it stored.
func (c *Compute) RefreshSpotPrices(ctx context.Context, logger *slog.Logger) (int, error) {
	stored := 0
	var failed error
	for _, region := range slices.Sorted(maps.Keys(c.fleet.Networks)) {
		types := spotTypes(region)
		zones := map[string]bool{}
		for _, s := range c.fleet.Networks[region].Subnets {
			if s.ZoneID != "" {
				zones[s.ZoneID] = true
			}
		}
		if len(types) == 0 || len(zones) == 0 {
			continue
		}
		client := c.aws().ec2(awsScope{key: string(KindPlatform)}, region)
		prices, err := spotQuotes(ctx, client, slices.Sorted(maps.Keys(zones)), types, time.Now())
		if err != nil {
			if ctx.Err() != nil {
				return stored, err
			}
			logger.WarnContext(ctx, "spot prices unavailable", "region", region, "error", err)
			failed = errors.Join(failed, fmt.Errorf("%s: %w", region, err))
			continue
		}
		params := UpsertSpotPricesParams{Region: region}
		for _, p := range prices {
			params.ZoneIds = append(params.ZoneIds, p.ZoneID)
			params.InstanceTypes = append(params.InstanceTypes, p.InstanceType)
			params.HourlyMicros = append(params.HourlyMicros, p.HourlyMicros)
			params.EffectiveAt = append(params.EffectiveAt, p.EffectiveAt)
		}
		if err := c.queries.UpsertSpotPrices(ctx, params); err != nil {
			return stored, fmt.Errorf("store spot prices: %w", err)
		}
		stored += len(prices)
		scores, err := placementScores(ctx, client, region, zones, types)
		if err != nil {
			if ctx.Err() != nil {
				return stored, err
			}
			logger.WarnContext(ctx, "spot placement scores incomplete", "region", region, "error", err)
			failed = errors.Join(failed, fmt.Errorf("%s placement scores: %w", region, err))
		}
		if len(scores.Scores) > 0 {
			if err := c.queries.ScoreSpotPools(ctx, scores); err != nil {
				return stored, fmt.Errorf("store spot placement scores: %w", err)
			}
		}
	}
	return stored, failed
}

// placementShape names the shape a type's pools are scored as: its vCPUs
// and GPUs.
func placementShape(t CatalogType) string {
	return fmt.Sprintf("%d/%s/%d", t.VCPUs(), t.GPU, t.GPUCount)
}

// placementScores reads AWS's Spot placement score of one instance in each
// of zones for every shape of types the catalog sells at least
// placementShapeTypes of, and gives each type its shape's score: a pool is
// scored as any catalog type of its vCPUs and GPUs. Shapes of fewer types,
// which include the 2- and 4-vCPU ones bought most, are not read: padding
// them with types the catalog never launches would score capacity it does
// not buy. Their pools carry no placement penalty and rank on full cost,
// and a refused pool moves the launch to the next. A shape whose read
// fails is left out and the others kept.
func placementScores(ctx context.Context, client *ec2.Client, region string, zones map[string]bool, types []string) (ScoreSpotPoolsParams, error) {
	shapes := map[string][]string{}
	for _, name := range types {
		if t, ok := CatalogTypeNamed(name); ok {
			shapes[placementShape(t)] = append(shapes[placementShape(t)], name)
		}
	}
	maps.DeleteFunc(shapes, func(_ string, types []string) bool { return len(types) < placementShapeTypes })
	keys := slices.Sorted(maps.Keys(shapes))
	scores := make([]map[string]int16, len(keys))
	errs := make([]error, len(keys))
	var group errgroup.Group
	group.SetLimit(placementScoreCalls)
	for n, key := range keys {
		group.Go(func() error {
			scores[n], errs[n] = shapeScores(ctx, client, region, shapes[key])
			return nil
		})
	}
	_ = group.Wait() // Each read records its own error.
	params := ScoreSpotPoolsParams{Region: region}
	for n, key := range keys {
		for _, zone := range slices.Sorted(maps.Keys(scores[n])) {
			if !zones[zone] {
				continue
			}
			for _, name := range shapes[key] {
				params.ZoneIds = append(params.ZoneIds, zone)
				params.InstanceTypes = append(params.InstanceTypes, name)
				params.Scores = append(params.Scores, scores[n][zone])
			}
		}
	}
	return params, errors.Join(errs...)
}

// shapeScores reads the placement score of one Spot instance of any of
// types in each zone of region.
func shapeScores(ctx context.Context, client *ec2.Client, region string, types []string) (map[string]int16, error) {
	out := map[string]int16{}
	pages := ec2.NewGetSpotPlacementScoresPaginator(client, &ec2.GetSpotPlacementScoresInput{
		InstanceTypes: types, TargetCapacity: aws.Int32(1), TargetCapacityUnitType: ec2types.TargetCapacityUnitTypeUnits,
		SingleAvailabilityZone: aws.Bool(true), RegionNames: []string{region},
	})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return nil, fmt.Errorf("get spot placement scores for %s: %w", strings.Join(types, ","), err)
		}
		for _, s := range page.SpotPlacementScores {
			score := aws.ToInt32(s.Score)
			if aws.ToString(s.Region) != region || aws.ToString(s.AvailabilityZoneId) == "" || score < 1 || score > 10 {
				return nil, fmt.Errorf("placement score %d for %s %s outside the request", score, aws.ToString(s.Region), aws.ToString(s.AvailabilityZoneId))
			}
			out[aws.ToString(s.AvailabilityZoneId)] = int16(score)
		}
	}
	return out, nil
}

// SpotPrices are the Spot prices fresh enough to buy on.
func (c *Compute) SpotPrices(ctx context.Context) ([]SpotQuote, error) {
	return readSpotPrices(ctx, c.queries)
}

// readSpotPrices are the Spot prices fresh enough to buy on, read through q.
func readSpotPrices(ctx context.Context, q *Queries) ([]SpotQuote, error) {
	rows, err := q.FreshSpotPrices(ctx, FreshSpotPricesParams{
		MaxAgeSeconds: spotPriceFresh.Seconds(), ScoreAgeSeconds: placementScoreFresh.Seconds(),
	})
	if err != nil {
		return nil, fmt.Errorf("read spot prices: %w", err)
	}
	out := make([]SpotQuote, len(rows))
	for n, r := range rows {
		out[n] = SpotQuote{
			Region: r.Region, ZoneID: r.AvailabilityZoneID, InstanceType: r.InstanceType, HourlyMicros: r.HourlyMicros,
			EffectiveAt: r.EffectiveAt, ObservedAt: r.ObservedAt, PlacementScore: int(r.PlacementScore),
		}
	}
	return out, nil
}

// spotTypes are the catalog types sold in region.
func spotTypes(region string) []string {
	var out []string
	for _, t := range FleetCatalog() {
		if _, sold := t.OnDemandMicros(region); sold {
			out = append(out, t.Name)
		}
	}
	return out
}

// spotQuotes reads the price in effect at now for types in each zone,
// keeping the latest per zone and type. A quote outside what was asked
// fails the region rather than storing a wrong price.
func spotQuotes(ctx context.Context, client *ec2.Client, zones, types []string, now time.Time) ([]SpotQuote, error) {
	instanceTypes := make([]ec2types.InstanceType, len(types))
	for n, t := range types {
		instanceTypes[n] = ec2types.InstanceType(t)
	}
	latest := map[[2]string]SpotQuote{}
	for _, zone := range zones {
		pages := ec2.NewDescribeSpotPriceHistoryPaginator(client, &ec2.DescribeSpotPriceHistoryInput{
			AvailabilityZoneId: aws.String(zone), InstanceTypes: instanceTypes,
			ProductDescriptions: []string{spotProduct}, StartTime: aws.Time(now), MaxResults: aws.Int32(1000),
		})
		for pages.HasMorePages() {
			page, err := pages.NextPage(ctx)
			if err != nil {
				return nil, fmt.Errorf("describe spot price history in %s: %w", zone, err)
			}
			for _, p := range page.SpotPriceHistory {
				name := string(p.InstanceType)
				if aws.ToString(p.AvailabilityZoneId) != zone || !slices.Contains(types, name) ||
					string(p.ProductDescription) != spotProduct || p.Timestamp == nil {
					return nil, fmt.Errorf("spot price for %s %s outside the request", name, aws.ToString(p.AvailabilityZoneId))
				}
				micros, err := dollarMicros(aws.ToString(p.SpotPrice))
				if err != nil || micros <= 0 {
					return nil, fmt.Errorf("spot price %q for %s in %s", aws.ToString(p.SpotPrice), name, zone)
				}
				key := [2]string{zone, name}
				if prior, ok := latest[key]; ok && !p.Timestamp.After(prior.EffectiveAt) {
					continue
				}
				latest[key] = SpotQuote{
					ZoneID: zone, InstanceType: name, HourlyMicros: micros,
					EffectiveAt: *p.Timestamp, ObservedAt: now,
				}
			}
		}
	}
	out := make([]SpotQuote, 0, len(latest))
	for _, key := range slices.SortedFunc(maps.Keys(latest), func(a, b [2]string) int {
		return cmp.Or(cmp.Compare(a[0], b[0]), cmp.Compare(a[1], b[1]))
	}) {
		out = append(out, latest[key])
	}
	return out, nil
}

// dollarMicros reads a decimal dollar amount such as "0.033100" as USD
// micros, rounding a finer fraction up.
func dollarMicros(s string) (int64, error) {
	whole, fraction, _ := strings.Cut(s, ".")
	if whole == "" {
		whole = "0"
	}
	if strings.Trim(fraction, "0123456789") != "" {
		return 0, fmt.Errorf("price %q is not a decimal", s)
	}
	dollars, err := strconv.ParseInt(whole, 10, 64)
	if err != nil || dollars < 0 || dollars > math.MaxInt64/1_000_000-1 {
		return 0, fmt.Errorf("price %q is not a decimal", s)
	}
	padded := fraction + "000000"
	micros, err := strconv.ParseInt(padded[:6], 10, 64)
	if err != nil {
		return 0, fmt.Errorf("price %q is not a decimal", s)
	}
	if strings.Trim(padded[6:], "0") != "" {
		micros++
	}
	return dollars*1e6 + micros, nil
}

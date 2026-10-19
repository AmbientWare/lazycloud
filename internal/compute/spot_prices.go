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
	"github.com/jackc/pgx/v5"
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
// type, every region in one transaction, so planning never prices from
// some regions' fresh quotes beside others' stale ones; then each priced
// pool's placement score. A region whose read fails keeps its last prices
// or scores. It reports how many prices it stored.
func (c *Compute) RefreshSpotPrices(ctx context.Context, logger *slog.Logger) (int, error) {
	stored, zones, failed, err := c.refreshPrices(ctx, logger)
	if err != nil || len(zones) == 0 {
		return stored, err
	}
	scores, err := placementScores(ctx, c.aws().ec2(c.platformAccount(false).scope, slices.Sorted(maps.Keys(zones))[0]), zones)
	if err != nil {
		if ctx.Err() != nil {
			return stored, err
		}
		logger.WarnContext(ctx, "spot placement scores incomplete", "error", err)
		failed = errors.Join(failed, fmt.Errorf("placement scores: %w", err))
	}
	if len(scores.Scores) > 0 {
		if err := c.queries.ScoreSpotPools(ctx, scores); err != nil {
			return stored, fmt.Errorf("store spot placement scores: %w", err)
		}
	}
	return stored, failed
}

// refreshPrices reads every region's Spot prices, then stores them in one
// transaction. It returns the fleet's zones by region and the reads that
// failed apart from err, which stopped it.
func (c *Compute) refreshPrices(ctx context.Context, logger *slog.Logger) (int, map[string]map[string]bool, error, error) {
	zones, err := c.fleetZones(ctx)
	if err != nil {
		return 0, nil, nil, err
	}
	var updates []UpsertSpotPricesParams
	var failed error
	for _, region := range slices.Sorted(maps.Keys(zones)) {
		types := spotTypes(region)
		if len(types) == 0 {
			continue
		}
		client := c.aws().ec2(c.platformAccount(false).scope, region)
		prices, err := spotQuotes(ctx, client, slices.Sorted(maps.Keys(zones[region])), types, time.Now())
		if err != nil {
			if ctx.Err() != nil {
				return 0, nil, nil, err
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
		updates = append(updates, params)
	}
	stored := 0
	err = inTx(ctx, c, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		for _, params := range updates {
			if err := q.UpsertSpotPrices(ctx, params); err != nil {
				return fmt.Errorf("store spot prices: %w", err)
			}
			stored += len(params.ZoneIds)
		}
		return nil
	})
	if err != nil {
		return 0, nil, nil, err
	}
	return stored, zones, failed, nil
}

// placementShape names what a type's pools are scored as: G and P types
// alone, which AWS scores one by one; any other type as its shape, its
// vCPUs, which AWS scores only as three or more types.
func placementShape(t CatalogType) string {
	if class, _ := QuotaClassOf(t.Name); class == QuotaG || class == QuotaP {
		return t.Name
	}
	return fmt.Sprintf("%d vCPUs", t.VCPUs())
}

// placementScores reads AWS's Spot placement score of one instance in every
// zone of zones, by region, for each shape of the catalog types those
// regions sell: each G and P type alone, and every other shape of at least
// placementShapeTypes types. Shapes of fewer types, which include the 2-
// and 4-vCPU ones, are not read: padding them with types the catalog never
// launches would score capacity it does not buy. One read covers every
// fleet region whether or not its prices were read, so the reads are one
// fixed configuration per shape, which AWS repeats freely; it limits new
// configurations a day. A shape whose read fails is left out and the
// others kept.
func placementScores(ctx context.Context, client *ec2.Client, zones map[string]map[string]bool) (ScoreSpotPoolsParams, error) {
	regions := slices.Sorted(maps.Keys(zones))
	all := map[string]bool{}
	sold := map[string]bool{}
	for _, region := range regions {
		maps.Copy(all, zones[region])
		for _, t := range spotTypes(region) {
			sold[t] = true
		}
	}
	shapes := map[string][]string{}
	for _, t := range FleetCatalog() {
		if sold[t.Name] {
			shapes[placementShape(t)] = append(shapes[placementShape(t)], t.Name)
		}
	}
	maps.DeleteFunc(shapes, func(shape string, types []string) bool {
		return len(types) < placementShapeTypes && shape != types[0]
	})
	keys := slices.Sorted(maps.Keys(shapes))
	scores := make([]map[string]int16, len(keys))
	errs := make([]error, len(keys))
	var group errgroup.Group
	group.SetLimit(placementScoreCalls)
	for n, key := range keys {
		group.Go(func() error {
			scores[n], errs[n] = shapeScores(ctx, client, regions, all, shapes[key])
			return nil
		})
	}
	_ = group.Wait() // Each read records its own error.
	var params ScoreSpotPoolsParams
	for n, key := range keys {
		for _, zone := range slices.Sorted(maps.Keys(scores[n])) {
			if !all[zone] {
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

// placementScoreZones is the most zones AWS returns for one placement score
// read, the best scored, without a next page.
const placementScoreZones = 10

// shapeScores reads the placement score of one Spot instance of any of
// types in each of zones, which lie in regions. When AWS returns its most
// zones, a zone it left out scores no higher than the lowest it returned,
// which the zone gets; a zone that sells none of types has no price to
// score.
func shapeScores(ctx context.Context, client *ec2.Client, regions []string, zones map[string]bool, types []string) (map[string]int16, error) {
	out, err := client.GetSpotPlacementScores(ctx, &ec2.GetSpotPlacementScoresInput{
		InstanceTypes: types, TargetCapacity: aws.Int32(1), TargetCapacityUnitType: ec2types.TargetCapacityUnitTypeUnits,
		SingleAvailabilityZone: aws.Bool(true), RegionNames: regions,
	})
	if err != nil {
		return nil, fmt.Errorf("get spot placement scores for %s: %w", strings.Join(types, ","), err)
	}
	scores := map[string]int16{}
	lowest := int16(10)
	for _, s := range out.SpotPlacementScores {
		score := aws.ToInt32(s.Score)
		if !slices.Contains(regions, aws.ToString(s.Region)) || aws.ToString(s.AvailabilityZoneId) == "" || score < 1 || score > 10 {
			return nil, fmt.Errorf("placement score %d for %s %s outside the request", score, aws.ToString(s.Region), aws.ToString(s.AvailabilityZoneId))
		}
		scores[aws.ToString(s.AvailabilityZoneId)] = int16(score)
		lowest = min(lowest, int16(score))
	}
	if len(out.SpotPlacementScores) < placementScoreZones {
		return scores, nil
	}
	for zone := range zones {
		if _, scored := scores[zone]; !scored {
			scores[zone] = lowest
		}
	}
	return scores, nil
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

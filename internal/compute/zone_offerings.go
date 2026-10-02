package compute

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"slices"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/jackc/pgx/v5"
)

// RefreshZoneOfferings reads which catalog types EC2 offers in each zone
// of the platform's networks, one call and one transaction per region. EC2
// sells some types in only some zones of a region (on 2026-10-02 use1-az3
// offered 2 of the catalog's 55), and a launch into a zone without its type
// is refused. A region whose read fails keeps its last offerings. It
// reports how many zone and type pairs it stored.
func (c *Compute) RefreshZoneOfferings(ctx context.Context, logger *slog.Logger) (int, error) {
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
		params, err := describeZoneOfferings(ctx, c.aws().ec2(awsScope{key: string(KindPlatform)}, region), region, zones, types)
		if err != nil {
			if ctx.Err() != nil {
				return stored, err
			}
			logger.WarnContext(ctx, "zone offerings unavailable", "region", region, "error", err)
			failed = errors.Join(failed, fmt.Errorf("%s: %w", region, err))
			continue
		}
		if err := inTx(ctx, c, func(tx pgx.Tx) error {
			q := c.queries.WithTx(tx)
			if err := q.UpsertZoneOfferings(ctx, params); err != nil {
				return fmt.Errorf("store zone offerings: %w", err)
			}
			if err := q.DropStaleZoneOfferings(ctx, region); err != nil {
				return fmt.Errorf("drop stale zone offerings: %w", err)
			}
			return nil
		}); err != nil {
			return stored, err
		}
		stored += len(params.ZoneIds)
	}
	return stored, failed
}

func describeZoneOfferings(ctx context.Context, client *ec2.Client, region string, zones map[string]bool, types []string) (UpsertZoneOfferingsParams, error) {
	params := UpsertZoneOfferingsParams{Region: region}
	pages := ec2.NewDescribeInstanceTypeOfferingsPaginator(client, &ec2.DescribeInstanceTypeOfferingsInput{
		LocationType: ec2types.LocationTypeAvailabilityZoneId,
		Filters: []ec2types.Filter{
			{Name: aws.String("instance-type"), Values: types},
			{Name: aws.String("location"), Values: slices.Sorted(maps.Keys(zones))},
		},
	})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return params, fmt.Errorf("describe instance type offerings: %w", err)
		}
		for _, o := range page.InstanceTypeOfferings {
			zone, name := aws.ToString(o.Location), string(o.InstanceType)
			if !zones[zone] || !slices.Contains(types, name) {
				return params, fmt.Errorf("offering of %s in %s outside the request", name, zone)
			}
			params.ZoneIds = append(params.ZoneIds, zone)
			params.InstanceTypes = append(params.InstanceTypes, name)
		}
	}
	return params, nil
}

// readZoneOfferings are the types each zone offers, by region and zone
// id, read through q. A region never read is absent, which limits nothing.
func readZoneOfferings(ctx context.Context, q *Queries) (map[string]map[string][]string, error) {
	rows, err := q.ZoneOfferings(ctx)
	if err != nil {
		return nil, fmt.Errorf("read zone offerings: %w", err)
	}
	out := map[string]map[string][]string{}
	for _, r := range rows {
		if out[r.Region] == nil {
			out[r.Region] = map[string][]string{}
		}
		out[r.Region][r.AvailabilityZoneID] = r.InstanceTypes
	}
	return out, nil
}

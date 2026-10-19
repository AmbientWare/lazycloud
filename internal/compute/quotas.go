package compute

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"math"
	"slices"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/servicequotas"
	"github.com/google/uuid"
)

const (
	// ZoneOfferingInterval paces the zone offering read, which changes
	// when AWS adds a type to a zone.
	ZoneOfferingInterval = time.Hour
	// quotaRecheck is how often an account's quotas are read again, so an
	// increase AWS approves counts within it. A newly connected account's
	// are read once its networks are.
	quotaRecheck = 15 * time.Minute
	// quotaFresh is how long a quota read stays good; an older one is
	// unknown, which bounds nothing.
	quotaFresh = 3 * time.Hour
)

// QuotaClass is the EC2 vCPU quota an instance type counts against.
type QuotaClass string

const (
	// QuotaStandard covers the A, C, D, H, I, M, R, T and Z families.
	QuotaStandard QuotaClass = "standard"
	// QuotaG covers the G and VT families.
	QuotaG QuotaClass = "g"
	// QuotaP covers the P family.
	QuotaP QuotaClass = "p"
)

// quotaCodes are the Service Quotas codes of each class and market.
func quotaCodes() map[QuotaClass]map[Market]string {
	return map[QuotaClass]map[Market]string{
		QuotaStandard: {MarketOnDemand: "L-1216C47A", MarketSpot: "L-34B43A08"},
		QuotaG:        {MarketOnDemand: "L-DB2E81BA", MarketSpot: "L-3819A6DF"},
		QuotaP:        {MarketOnDemand: "L-417A185B", MarketSpot: "L-7212CCBC"},
	}
}

// QuotaClassOf is the quota class of an instance type, by its family.
func QuotaClassOf(instanceType string) (QuotaClass, bool) {
	family, _, _ := strings.Cut(instanceType, ".")
	switch {
	case family == "":
		return "", false
	case strings.HasPrefix(family, "vt") || family[0] == 'g':
		return QuotaG, true
	case family[0] == 'p':
		return QuotaP, true
	case strings.HasPrefix(family, "inf") || strings.HasPrefix(family, "trn") || strings.HasPrefix(family, "dl"):
		// Each has its own quota, which the fleet does not buy.
		return "", false
	case strings.ContainsRune("acdhimrtz", rune(family[0])):
		return QuotaStandard, true
	}
	return "", false
}

// QuotaRoom is an account's vCPU quota in one region, class and market as
// the last read left it. The planner subtracts what the account runs.
type QuotaRoom struct {
	Region string
	Class  QuotaClass
	Market Market
	// Known is false when the quota was never read or the read is stale;
	// VCPUs then bounds nothing.
	Known bool
	// VCPUs is the quota; zero while a quota refusal cools the class.
	VCPUs int64
	// Refused is a quota refusal cooling the class.
	Refused bool
}

// RefreshQuotas reads the EC2 vCPU quotas of each managed account in each
// of its regions that were never read or were read longer than
// quotaRecheck ago, one statement per account and region. A region whose
// read fails keeps its last quotas. It reports how many it stored.
func (c *Compute) RefreshQuotas(ctx context.Context, logger *slog.Logger) (int, error) {
	accounts, err := c.accounts(ctx, c.queries)
	if err != nil {
		return 0, err
	}
	rows, err := c.queries.Quotas(ctx)
	if err != nil {
		return 0, fmt.Errorf("read quotas: %w", err)
	}
	type place struct{ owner, region string }
	fresh := map[place]int{}
	for _, r := range rows {
		if r.ObservedAt != nil && time.Since(*r.ObservedAt) < quotaRecheck {
			fresh[place{r.Owner, r.Region}]++
		}
	}
	read := 0
	for _, codes := range quotaCodes() {
		read += len(codes)
	}
	stored := 0
	var failed error
	for _, a := range accounts {
		if !a.managed {
			continue
		}
		for _, region := range slices.Sorted(maps.Keys(a.networks)) {
			if fresh[place{a.key(), region}] == read {
				continue
			}
			params, err := c.readQuotas(ctx, a, region)
			if err != nil {
				if ctx.Err() != nil {
					return stored, err
				}
				logger.WarnContext(ctx, "ec2 quotas unavailable", "owner", a.key(), "region", region, "error", err)
				failed = errors.Join(failed, fmt.Errorf("%s %s: %w", a.key(), region, err))
				continue
			}
			if err := c.queries.UpsertQuotas(ctx, params); err != nil {
				return stored, fmt.Errorf("store quotas: %w", err)
			}
			stored += len(params.Vcpus)
		}
	}
	return stored, failed
}

// readQuotas reads one account's quotas in region.
func (c *Compute) readQuotas(ctx context.Context, a account, region string) (UpsertQuotasParams, error) {
	codes := quotaCodes()
	client := c.aws().serviceQuotas(a.scope, region)
	params := UpsertQuotasParams{Owner: a.key(), Region: region}
	for _, class := range slices.Sorted(maps.Keys(codes)) {
		for _, market := range slices.Sorted(maps.Keys(codes[class])) {
			out, err := client.GetServiceQuota(ctx, &servicequotas.GetServiceQuotaInput{
				ServiceCode: aws.String("ec2"), QuotaCode: aws.String(codes[class][market]),
			})
			if err != nil {
				return params, fmt.Errorf("get quota %s: %w", codes[class][market], err)
			}
			if out.Quota == nil || out.Quota.Value == nil || *out.Quota.Value < 0 || *out.Quota.Value > math.MaxInt32 {
				return params, fmt.Errorf("quota %s has no vCPU value", codes[class][market])
			}
			params.Classes = append(params.Classes, string(class))
			params.Markets = append(params.Markets, string(market))
			params.Vcpus = append(params.Vcpus, int32(*out.Quota.Value))
		}
	}
	return params, nil
}

// quotaRooms are each account's vCPU quotas per region, class and market,
// by owner key, read through q.
func quotaRooms(ctx context.Context, q *Queries, now time.Time) (map[string][]QuotaRoom, error) {
	quotas, err := q.Quotas(ctx)
	if err != nil {
		return nil, fmt.Errorf("read quotas: %w", err)
	}
	out := map[string][]QuotaRoom{}
	for _, r := range quotas {
		room := QuotaRoom{Region: r.Region, Class: QuotaClass(r.QuotaClass), Market: Market(r.Market), Refused: r.Refused}
		if room.Known = r.Vcpus != nil && r.ObservedAt != nil && now.Sub(*r.ObservedAt) < quotaFresh; room.Known {
			room.VCPUs = int64(*r.Vcpus)
		}
		if room.Refused {
			room.Known, room.VCPUs = true, 0
		}
		out[r.Owner] = append(out[r.Owner], room)
	}
	return out, nil
}

// refuseQuota cools the quota class of instanceType in an account's region
// after EC2 refused it for quota.
func (c *Compute) refuseQuota(ctx context.Context, q *Queries, owner, region, instanceType string, market Market) error {
	class, ok := QuotaClassOf(instanceType)
	if !ok {
		return nil
	}
	if err := q.RefuseQuota(ctx, RefuseQuotaParams{
		Owner: owner, Region: region, QuotaClass: string(class), Market: string(market), Seconds: c.fleet.CapacityCooldown.Seconds(),
	}); err != nil {
		return fmt.Errorf("refuse quota: %w", err)
	}
	return nil
}

// BlockedQuota is a vCPU quota of a connected account too small for the
// smallest machine the fleet buys in its class, region and market, and the
// Service Quotas page where the customer asks AWS for more.
type BlockedQuota struct {
	Region string
	Class  QuotaClass
	Market Market
	// VCPUs is the quota; Needs the vCPUs of the smallest machine.
	VCPUs, Needs int64
	URL          string
}

// BlockedQuotas are the connection's standard and G quotas, as last read,
// that leave no room for one machine. P quotas start at zero in most
// accounts and only P work needs them.
func (c *Compute) BlockedQuotas(ctx context.Context, connection uuid.UUID) ([]BlockedQuota, error) {
	rooms, err := quotaRooms(ctx, c.queries, time.Now())
	if err != nil {
		return nil, err
	}
	codes := quotaCodes()
	var out []BlockedQuota
	for _, r := range rooms[hostOwner(&connection)] {
		if !r.Known || r.Refused || (r.Class != QuotaStandard && r.Class != QuotaG) {
			continue
		}
		needs := smallestVCPUs(r.Class, r.Region)
		if needs == 0 || r.VCPUs >= needs {
			continue
		}
		out = append(out, BlockedQuota{
			Region: r.Region, Class: r.Class, Market: r.Market, VCPUs: r.VCPUs, Needs: needs,
			URL: fmt.Sprintf("https://%s.console.aws.amazon.com/servicequotas/home/services/ec2/quotas/%s", r.Region, codes[r.Class][r.Market]),
		})
	}
	return out, nil
}

// smallestVCPUs is the vCPUs of the smallest catalog type of class sold in
// region, 0 for none.
func smallestVCPUs(class QuotaClass, region string) int64 {
	var smallest int64
	for _, t := range FleetCatalog() {
		if c, ok := QuotaClassOf(t.Name); !ok || c != class {
			continue
		}
		if _, sold := t.OnDemandMicros(region); sold && (smallest == 0 || t.VCPUs() < smallest) {
			smallest = t.VCPUs()
		}
	}
	return smallest
}

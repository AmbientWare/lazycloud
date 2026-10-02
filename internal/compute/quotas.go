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
)

const (
	// QuotaInterval paces the EC2 quota read.
	QuotaInterval = time.Hour
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

// QuotaRoom is the vCPU quota the platform has left in one region, class
// and market.
type QuotaRoom struct {
	Region string
	Class  QuotaClass
	Market Market
	// Known is false when the quota was never read or the read is stale;
	// VCPUs then bounds nothing.
	Known bool
	// VCPUs is the quota less the vCPUs of platform instances that hold it,
	// never below zero; zero while a quota refusal cools the class.
	VCPUs int64
	// Refused is a quota refusal cooling the class.
	Refused bool
}

// RefreshQuotas reads the platform account's EC2 vCPU quotas in every
// platform region, one statement per region. A region whose read fails
// keeps its last quotas. It reports how many it stored.
func (c *Compute) RefreshQuotas(ctx context.Context, logger *slog.Logger) (int, error) {
	stored := 0
	var failed error
	codes := quotaCodes()
	for _, region := range slices.Sorted(maps.Keys(c.fleet.Networks)) {
		client := c.aws().serviceQuotas(awsScope{key: string(KindPlatform)}, region)
		params := UpsertQuotasParams{Region: region}
		var regionErr error
		for _, class := range slices.Sorted(maps.Keys(codes)) {
			for _, market := range slices.Sorted(maps.Keys(codes[class])) {
				out, err := client.GetServiceQuota(ctx, &servicequotas.GetServiceQuotaInput{
					ServiceCode: aws.String("ec2"), QuotaCode: aws.String(codes[class][market]),
				})
				if err != nil {
					regionErr = fmt.Errorf("get quota %s: %w", codes[class][market], err)
					break
				}
				if out.Quota == nil || out.Quota.Value == nil || *out.Quota.Value < 0 || *out.Quota.Value > math.MaxInt32 {
					regionErr = fmt.Errorf("quota %s has no vCPU value", codes[class][market])
					break
				}
				params.Classes = append(params.Classes, string(class))
				params.Markets = append(params.Markets, string(market))
				params.Vcpus = append(params.Vcpus, int32(*out.Quota.Value))
			}
			if regionErr != nil {
				break
			}
		}
		if regionErr != nil {
			if ctx.Err() != nil {
				return stored, regionErr
			}
			logger.WarnContext(ctx, "ec2 quotas unavailable", "region", region, "error", regionErr)
			failed = errors.Join(failed, fmt.Errorf("%s: %w", region, regionErr))
			continue
		}
		if err := c.queries.UpsertQuotas(ctx, params); err != nil {
			return stored, fmt.Errorf("store quotas: %w", err)
		}
		stored += len(params.Vcpus)
	}
	return stored, failed
}

// QuotaRooms are the platform's quota room per region, class and market,
// from the last quota read less the platform instances that hold quota.
func (c *Compute) QuotaRooms(ctx context.Context) ([]QuotaRoom, error) {
	quotas, err := c.queries.Quotas(ctx)
	if err != nil {
		return nil, fmt.Errorf("read quotas: %w", err)
	}
	usage, err := c.queries.QuotaUsage(ctx)
	if err != nil {
		return nil, fmt.Errorf("read quota usage: %w", err)
	}
	used := map[[3]string]int64{}
	for _, u := range usage {
		class, ok := QuotaClassOf(u.InstanceType)
		vcpus, known := typeVCPUs(u.InstanceType)
		if !ok || !known {
			continue
		}
		used[[3]string{u.Region, string(class), u.Market}] += vcpus * int64(u.Hosts)
	}
	out := make([]QuotaRoom, len(quotas))
	for n, q := range quotas {
		r := QuotaRoom{Region: q.Region, Class: QuotaClass(q.QuotaClass), Market: Market(q.Market), Refused: q.Refused}
		r.Known = q.Vcpus != nil && q.ObservedAt != nil && time.Since(*q.ObservedAt) < quotaFresh
		if r.Known {
			r.VCPUs = max(0, int64(*q.Vcpus)-used[[3]string{q.Region, q.QuotaClass, q.Market}])
		}
		if r.Refused {
			r.Known, r.VCPUs = true, 0
		}
		out[n] = r
	}
	return out, nil
}

// refuseQuota cools the quota class of instanceType in region after EC2
// refused it for quota.
func (c *Compute) refuseQuota(ctx context.Context, q *Queries, region, instanceType string, market Market) error {
	class, ok := QuotaClassOf(instanceType)
	if !ok {
		return nil
	}
	if err := q.RefuseQuota(ctx, RefuseQuotaParams{
		Region: region, QuotaClass: string(class), Market: string(market), Seconds: c.fleet.CapacityCooldown.Seconds(),
	}); err != nil {
		return fmt.Errorf("refuse quota: %w", err)
	}
	return nil
}

// typeVCPUs is the vCPU count of a catalog type.
func typeVCPUs(instanceType string) (int64, bool) {
	for _, t := range catalog() {
		if t.Name == instanceType {
			return t.CPUMillis / 1000, true
		}
	}
	return 0, false
}

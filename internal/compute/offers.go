package compute

import (
	"slices"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/google/uuid"
)

// Fleet configures the platform's AWS capacity. Without networks the
// platform launches nothing, which is how a local stack runs.
type Fleet struct {
	// Name tags every instance the fleet launches, in every account, so
	// reconciliation finds them.
	Name string
	// AWS holds the platform's credentials and default region.
	AWS aws.Config
	// AccountID is the platform account; its instances enroll as NodeRoleARN.
	AccountID string
	// PrincipalARN is the platform principal connection roles trust.
	PrincipalARN string
	// NodeRoleARN and InstanceProfile are what platform instances run as.
	NodeRoleARN     string
	InstanceProfile string
	// Networks are the platform's launchable regions.
	Networks map[string]Network
	// MaxHosts bounds the live cloud hosts of the platform and of each
	// connected account.
	MaxHosts int
	// IdleTimeout is how long a ready instance may run no container before
	// it drains, beyond the headroom floor.
	IdleTimeout time.Duration
	// HeadroomFloor is how many idle ready instances each market keeps.
	HeadroomFloor int
	// BootTimeout is how long a launched instance may take to enroll.
	BootTimeout time.Duration
	// CapacityCooldown is how long an offer that lacked capacity is skipped.
	CapacityCooldown time.Duration
	// Endpoints override AWS service endpoints; tests point them at
	// recorded responses.
	Endpoints Endpoints
}

// Endpoints are AWS service base URLs; empty uses AWS.
type Endpoints struct {
	EC2            string
	STS            string
	CloudFormation string
}

// Network is where instances launch in one region.
type Network struct {
	VPCID           string   `json:"vpc_id"`
	SecurityGroupID string   `json:"security_group_id"`
	Subnets         []Subnet `json:"subnets"`
}

// Subnet is one launchable subnet and its zone.
type Subnet struct {
	ID     string `json:"id"`
	Zone   string `json:"zone"`
	ZoneID string `json:"zone_id"`
}

func (f Fleet) withDefaults() Fleet {
	if f.Name == "" {
		f.Name = "lazycloud"
	}
	if f.MaxHosts == 0 {
		f.MaxHosts = 20
	}
	if f.IdleTimeout == 0 {
		f.IdleTimeout = 5 * time.Minute
	}
	if f.BootTimeout == 0 {
		f.BootTimeout = 10 * time.Minute
	}
	if f.CapacityCooldown == 0 {
		f.CapacityCooldown = 10 * time.Minute
	}
	return f
}

// InstanceType is a catalog shape with its on-demand price in us-east-1.
type InstanceType struct {
	Name        string
	CPUMillis   int64
	MemoryBytes int64
	GPU         string
	GPUCount    int
	// HourlyMicros is the us-east-1 on-demand price in USD micros.
	HourlyMicros int64
	// Regions limits where the type is sold; nil means every fleet region.
	Regions []string
}

const gib = int64(1) << 30

// catalog is what the fleet buys. Prices are hand-reviewed on-demand rates;
// Spot is estimated from them at spotDiscount. us-west-1 sells none of the
// GPU types but T4.
func catalog() []InstanceType {
	gpuRegions := []string{"us-east-1", "us-east-2", "us-west-2"}
	return []InstanceType{
		{Name: "m7i.large", CPUMillis: 2000, MemoryBytes: 8 * gib, HourlyMicros: 100800},
		{Name: "m7i.xlarge", CPUMillis: 4000, MemoryBytes: 16 * gib, HourlyMicros: 201600},
		{Name: "m7i.2xlarge", CPUMillis: 8000, MemoryBytes: 32 * gib, HourlyMicros: 403200},
		{Name: "m7i.4xlarge", CPUMillis: 16000, MemoryBytes: 64 * gib, HourlyMicros: 806400},
		{Name: "m7i.8xlarge", CPUMillis: 32000, MemoryBytes: 128 * gib, HourlyMicros: 1612800},
		{Name: "c7i.2xlarge", CPUMillis: 8000, MemoryBytes: 16 * gib, HourlyMicros: 357000},
		{Name: "r7i.2xlarge", CPUMillis: 8000, MemoryBytes: 64 * gib, HourlyMicros: 529200},
		{Name: "g4dn.xlarge", CPUMillis: 4000, MemoryBytes: 16 * gib, GPU: "T4", GPUCount: 1, HourlyMicros: 526000},
		{Name: "g4dn.12xlarge", CPUMillis: 48000, MemoryBytes: 192 * gib, GPU: "T4", GPUCount: 4, HourlyMicros: 3912000},
		{Name: "g5.xlarge", CPUMillis: 4000, MemoryBytes: 16 * gib, GPU: "A10G", GPUCount: 1, HourlyMicros: 1006000, Regions: gpuRegions},
		{Name: "g5.12xlarge", CPUMillis: 48000, MemoryBytes: 192 * gib, GPU: "A10G", GPUCount: 4, HourlyMicros: 5672000, Regions: gpuRegions},
		{Name: "g6.xlarge", CPUMillis: 4000, MemoryBytes: 16 * gib, GPU: "L4", GPUCount: 1, HourlyMicros: 804800, Regions: gpuRegions},
		{Name: "g6.12xlarge", CPUMillis: 48000, MemoryBytes: 192 * gib, GPU: "L4", GPUCount: 4, HourlyMicros: 4601600, Regions: gpuRegions},
		{Name: "g6e.xlarge", CPUMillis: 4000, MemoryBytes: 32 * gib, GPU: "L40S", GPUCount: 1, HourlyMicros: 1861000, Regions: gpuRegions},
		{Name: "g6e.12xlarge", CPUMillis: 48000, MemoryBytes: 384 * gib, GPU: "L40S", GPUCount: 4, HourlyMicros: 10493000, Regions: gpuRegions},
		{Name: "p4d.24xlarge", CPUMillis: 96000, MemoryBytes: 1152 * gib, GPU: "A100-40", GPUCount: 8, HourlyMicros: 21957600, Regions: gpuRegions},
		{Name: "p4de.24xlarge", CPUMillis: 96000, MemoryBytes: 1152 * gib, GPU: "A100-80", GPUCount: 8, HourlyMicros: 27447000, Regions: []string{"us-east-1", "us-west-2"}},
		{Name: "p5.48xlarge", CPUMillis: 192000, MemoryBytes: 2048 * gib, GPU: "H100", GPUCount: 8, HourlyMicros: 55040000, Regions: gpuRegions},
		{Name: "p5en.48xlarge", CPUMillis: 192000, MemoryBytes: 2048 * gib, GPU: "H200", GPUCount: 8, HourlyMicros: 63296000, Regions: gpuRegions},
	}
}

// spotDiscount estimates Spot as a fraction of on-demand, for ordering
// offers only.
const spotDiscount = 0.4

// regionOrder is the purchase preference among the US regions.
func regionOrder() []string { return []string{"us-east-2", "us-west-1", "us-east-1", "us-west-2"} }

// regionPremium is us-west-1's price over us-east-1, in percent.
const regionPremium = 15

// Offer is one way to buy a host.
type Offer struct {
	Type   InstanceType
	Region string
	Zone   string
	ZoneID string
	Market Market
	// HourlyMicros is the expected price.
	HourlyMicros int64
}

// usable is what an instance offers containers once the agent's reserve
// and the kernel's share are taken, matching the agent's own detection.
func (o Offer) usable() (int64, int64) {
	cpu := o.Type.CPUMillis
	memory := o.Type.MemoryBytes * 94 / 100
	return cpu - max(500, cpu/10), memory - max(512<<20, memory/10)
}

// capacity is the offer as a host the controller can pack onto.
func (o Offer) capacity(target Target) HostCapacity {
	cpu, memory := o.usable()
	return HostCapacity{
		Kind: target.Kind, Provider: ProviderAWS, Connection: target.Connection, Region: o.Region, Zone: o.Zone, ZoneID: o.ZoneID,
		Market: o.Market, GPUType: o.Type.GPU, GPUCount: o.Type.GPUCount,
		CPUMillis: cpu, MemoryBytes: memory, FreeCPUMillis: cpu, FreeMemoryBytes: memory, FreeGPUs: o.Type.GPUCount,
	}
}

// Target is whose capacity a host is: the platform's or a connection's.
type Target struct {
	Kind       HostKind
	Connection *uuid.UUID
}

func (t Target) key() string {
	if t.Connection != nil {
		return t.Connection.String()
	}
	return string(KindPlatform)
}

// offersFor lists the offers that can host r in the given regions, best
// first: preferred GPU models, then price. Offers in cooldown are skipped.
func offersFor(r Requirement, regions map[string]Network, cooled func(region, instanceType string, market Market) bool) []Offer {
	var offers []Offer
	gpus := r.GPUsNeeded()
	for _, region := range regionOrder() {
		network, ok := regions[region]
		if !ok || (r.Region != "" && ProductRegion(region) != r.Region) {
			continue
		}
		var zone Subnet
		if r.Zone != "" {
			var ok bool
			if zone, ok = zoneIn(network, r.Zone); !ok {
				continue
			}
		}
		for _, t := range catalog() {
			if t.Regions != nil && !slices.Contains(t.Regions, region) {
				continue
			}
			if (gpus > 0) != (t.GPUCount > 0) || t.GPUCount < gpus || (gpus > 0 && !GPUAccepted(r.GPUs, t.GPU)) {
				continue
			}
			markets := []Market{MarketOnDemand}
			if r.Preemptible {
				markets = []Market{MarketSpot, MarketOnDemand}
			}
			for _, market := range markets {
				if cooled(region, t.Name, market) {
					continue
				}
				o := Offer{Type: t, Region: region, Zone: zone.Zone, ZoneID: zone.ZoneID, Market: market, HourlyMicros: price(t, region, market)}
				if cpu, memory := o.usable(); cpu < r.CPUMillis || memory < r.MemoryBytes {
					continue
				}
				offers = append(offers, o)
			}
		}
	}
	slices.SortStableFunc(offers, func(a, b Offer) int {
		if ra, rb := GPURank(r.GPUs, a.Type.GPU), GPURank(r.GPUs, b.Type.GPU); ra != rb {
			return ra - rb
		}
		switch {
		case a.HourlyMicros < b.HourlyMicros:
			return -1
		case a.HourlyMicros > b.HourlyMicros:
			return 1
		}
		return 0
	})
	return offers
}

func price(t InstanceType, region string, market Market) int64 {
	p := t.HourlyMicros
	if region == "us-west-1" {
		p += p * regionPremium / 100
	}
	if market == MarketSpot {
		p = int64(float64(p) * spotDiscount)
	}
	return p
}

// zoneIn returns a subnet of network in the zone a name or id names.
func zoneIn(network Network, zone string) (Subnet, bool) {
	for _, s := range network.Subnets {
		if s.Zone == zone || s.ZoneID == zone {
			return s, true
		}
	}
	return Subnet{}, false
}

// subnetFor picks the subnet to launch in: one in zone when set, otherwise
// the one after the host id's position, which spreads hosts over zones.
func subnetFor(network Network, zone string, host uuid.UUID) (Subnet, bool) {
	var candidates []Subnet
	for _, s := range network.Subnets {
		if zone == "" || strings.EqualFold(s.Zone, zone) {
			candidates = append(candidates, s)
		}
	}
	if len(candidates) == 0 {
		return Subnet{}, false
	}
	return candidates[int(host[15])%len(candidates)], true
}

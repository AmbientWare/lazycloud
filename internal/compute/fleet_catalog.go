package compute

import (
	"slices"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// hibernationMemoryLimit is the RAM EC2 hibernates instances under.
const hibernationMemoryLimit = 150 * gib

// CatalogType is an instance type the platform fleet buys.
type CatalogType struct {
	Name string
	// Architecture is the host architecture the type runs, as Go names it.
	Architecture string
	// Topology is the type's hardware threads, which EC2 sells and its
	// quotas count, and the cores they run on.
	Topology    cpu.Topology
	MemoryBytes int64
	GPU         string
	GPUCount    int
	// Hibernates is EC2's HibernationSupported for the type in every
	// region it is sold in; RAM is under 150 GiB.
	Hibernates bool
	// prices are reviewed on-demand hourly USD micros in regionOrder; zero
	// means EC2 does not sell the type in that region.
	prices [4]int64
}

// OnDemandMicros is the reviewed hourly on-demand price in region, and
// whether EC2 sells the type there.
func (t CatalogType) OnDemandMicros(region string) (int64, bool) {
	n := slices.Index(regionOrder(), region)
	if n < 0 || t.prices[n] == 0 {
		return 0, false
	}
	return t.prices[n], true
}

// CPUMillis is the type's size in CPUs.
func (t CatalogType) CPUMillis() cpu.Millis { return t.Topology.CPUMillis() }

// VCPUs is what a running host of the type counts against its quota.
func (t CatalogType) VCPUs() int64 { return int64(t.Topology.Threads) }

// Usable is what a host of the type offers containers, as the agent computes
// it (agent/capacity.go): every core, and memory less the larger of a floor
// and a tenth. Memory is 94% of nominal until a host of the type has
// reported, then what it reported.
func (t CatalogType) Usable(reportedMemory int64) FleetCapacity {
	memory := reportedMemory
	if memory <= 0 {
		nominal := t.MemoryBytes * 94 / 100
		memory = nominal - max(512<<20, nominal/10)
	}
	return FleetCapacity{CPUMillis: t.CPUMillis(), MemoryBytes: memory, GPUs: t.GPUCount}
}

// RootGiB is the root volume a host of the type launches with: a
// hibernating reserve adds its RAM for the swap file it hibernates to.
func (t CatalogType) RootGiB(hibernate bool) int64 {
	if hibernate && t.Hibernates {
		return rootVolumeGiB + (t.MemoryBytes+gib-1)/gib
	}
	return rootVolumeGiB
}

// twoPerCore is the topology of vcpus hardware threads, two on each core.
func twoPerCore(vcpus int) cpu.Topology { return cpu.Topology{Threads: vcpus, Cores: vcpus / 2} }

// CatalogTypeNamed finds a catalog type by name.
func CatalogTypeNamed(name string) (CatalogType, bool) {
	for _, t := range FleetCatalog() {
		if t.Name == name {
			return t, true
		}
	}
	return CatalogType{}, false
}

// FleetArchitectures are the host architectures the fleet catalog sells.
func FleetArchitectures() []string {
	var architectures []string
	for _, t := range FleetCatalog() {
		if !slices.Contains(architectures, t.Architecture) {
			architectures = append(architectures, t.Architecture)
		}
	}
	return architectures
}

// regionRates are a region's gp3 and public IPv4 rates in USD micros.
type regionRates struct {
	gp3GiBMonth, ipv4Hour int64
}

func ratesIn(region string) regionRates {
	if region == "us-west-1" {
		return regionRates{gp3GiBMonth: 96_000, ipv4Hour: 5_000}
	}
	return regionRates{gp3GiBMonth: 80_000, ipv4Hour: 5_000}
}

// rootDiskMicros is gib of gp3 for an hour, on AWS's 30-day month.
func rootDiskMicros(region string, gib int64) int64 {
	return (gib*ratesIn(region).gp3GiBMonth + 719) / 720
}

// FleetCatalog is what the platform fleet buys, with on-demand prices in
// us-east-2, us-west-1, us-east-1 and us-west-2 as the AWS price list
// published 2026-09-25 has them (testdata/fleet/on_demand_prices.json).
// Every type runs two threads per core, its DefaultThreadsPerCore.
func FleetCatalog() []CatalogType {
	cpuType := func(name string, vcpus int, memGiB int64, hibernates bool, prices [4]int64) CatalogType {
		return CatalogType{Name: name, Architecture: "amd64", Topology: twoPerCore(vcpus), MemoryBytes: memGiB * gib, Hibernates: hibernates, prices: prices}
	}
	gpu := func(name string, vcpus int, memGiB int64, model string, cards int, prices [4]int64) CatalogType {
		return CatalogType{Name: name, Architecture: "amd64", Topology: twoPerCore(vcpus), MemoryBytes: memGiB * gib, GPU: model, GPUCount: cards, prices: prices}
	}
	return []CatalogType{
		cpuType("m7i.large", 2, 8, true, [4]int64{100800, 117600, 100800, 100800}),
		cpuType("m7i.xlarge", 4, 16, true, [4]int64{201600, 235200, 201600, 201600}),
		cpuType("c6a.2xlarge", 8, 16, true, [4]int64{306000, 381600, 306000, 306000}),
		cpuType("c7i.2xlarge", 8, 16, true, [4]int64{357000, 445200, 357000, 357000}),
		cpuType("m6a.2xlarge", 8, 32, true, [4]int64{345600, 403200, 345600, 345600}),
		cpuType("m7i.2xlarge", 8, 32, true, [4]int64{403200, 470400, 403200, 403200}),
		cpuType("r6a.2xlarge", 8, 64, true, [4]int64{453600, 504000, 453600, 453600}),
		cpuType("r7i.2xlarge", 8, 64, true, [4]int64{529200, 588000, 529200, 529200}),
		cpuType("c6a.4xlarge", 16, 32, true, [4]int64{612000, 763200, 612000, 612000}),
		cpuType("m6a.4xlarge", 16, 64, true, [4]int64{691200, 806400, 691200, 691200}),
		cpuType("m7i.4xlarge", 16, 64, true, [4]int64{806400, 940800, 806400, 806400}),
		cpuType("r6a.4xlarge", 16, 128, true, [4]int64{907200, 1008000, 907200, 907200}),
		cpuType("c6a.8xlarge", 32, 64, true, [4]int64{1224000, 1526400, 1224000, 1224000}),
		cpuType("c6i.8xlarge", 32, 64, true, [4]int64{1360000, 1696000, 1360000, 1360000}),
		cpuType("m6a.8xlarge", 32, 128, true, [4]int64{1382400, 1612800, 1382400, 1382400}),
		cpuType("m7i.8xlarge", 32, 128, true, [4]int64{1612800, 1881600, 1612800, 1612800}),
		cpuType("r6a.8xlarge", 32, 256, false, [4]int64{1814400, 2016000, 1814400, 1814400}),
		cpuType("m7i.12xlarge", 48, 192, false, [4]int64{2419200, 2822400, 2419200, 2419200}),
		cpuType("m7i.16xlarge", 64, 256, false, [4]int64{3225600, 3763200, 3225600, 3225600}),
		gpu("g4dn.xlarge", 4, 16, "T4", 1, [4]int64{526000, 631000, 526000, 526000}),
		gpu("g4dn.2xlarge", 8, 32, "T4", 1, [4]int64{752000, 902000, 752000, 752000}),
		gpu("g4dn.4xlarge", 16, 64, "T4", 1, [4]int64{1204000, 1445000, 1204000, 1204000}),
		gpu("g4dn.8xlarge", 32, 128, "T4", 1, [4]int64{2176000, 2611000, 2176000, 2176000}),
		gpu("g4dn.16xlarge", 64, 256, "T4", 1, [4]int64{4352000, 5222000, 4352000, 4352000}),
		gpu("g4dn.12xlarge", 48, 192, "T4", 4, [4]int64{3912000, 4694000, 3912000, 3912000}),
		gpu("g4dn.metal", 96, 384, "T4", 8, [4]int64{7824000, 9389000, 7824000, 7824000}),
		gpu("g5.xlarge", 4, 16, "A10G", 1, [4]int64{1006000, 0, 1006000, 1006000}),
		gpu("g5.2xlarge", 8, 32, "A10G", 1, [4]int64{1212000, 0, 1212000, 1212000}),
		gpu("g5.4xlarge", 16, 64, "A10G", 1, [4]int64{1624000, 0, 1624000, 1624000}),
		gpu("g5.8xlarge", 32, 128, "A10G", 1, [4]int64{2448000, 0, 2448000, 2448000}),
		gpu("g5.16xlarge", 64, 256, "A10G", 1, [4]int64{4096000, 0, 4096000, 4096000}),
		gpu("g5.12xlarge", 48, 192, "A10G", 4, [4]int64{5672000, 0, 5672000, 5672000}),
		gpu("g5.24xlarge", 96, 384, "A10G", 4, [4]int64{8144000, 0, 8144000, 8144000}),
		gpu("g5.48xlarge", 192, 768, "A10G", 8, [4]int64{16288000, 0, 16288000, 16288000}),
		gpu("g6.xlarge", 4, 16, "L4", 1, [4]int64{804800, 0, 804800, 804800}),
		gpu("g6.2xlarge", 8, 32, "L4", 1, [4]int64{977600, 0, 977600, 977600}),
		gpu("g6.4xlarge", 16, 64, "L4", 1, [4]int64{1323200, 0, 1323200, 1323200}),
		gpu("g6.8xlarge", 32, 128, "L4", 1, [4]int64{2014400, 0, 2014400, 2014400}),
		gpu("g6.16xlarge", 64, 256, "L4", 1, [4]int64{3396800, 0, 3396800, 3396800}),
		gpu("g6.12xlarge", 48, 192, "L4", 4, [4]int64{4601600, 0, 4601600, 4601600}),
		gpu("g6.24xlarge", 96, 384, "L4", 4, [4]int64{6675200, 0, 6675200, 6675200}),
		gpu("g6.48xlarge", 192, 768, "L4", 8, [4]int64{13350400, 0, 13350400, 13350400}),
		gpu("g6e.xlarge", 4, 32, "L40S", 1, [4]int64{1861000, 0, 1861000, 1861000}),
		gpu("g6e.2xlarge", 8, 64, "L40S", 1, [4]int64{2242080, 0, 2242080, 2242080}),
		gpu("g6e.4xlarge", 16, 128, "L40S", 1, [4]int64{3004240, 0, 3004240, 3004240}),
		gpu("g6e.8xlarge", 32, 256, "L40S", 1, [4]int64{4528560, 0, 4528560, 4528560}),
		gpu("g6e.16xlarge", 64, 512, "L40S", 1, [4]int64{7577190, 0, 7577190, 7577190}),
		gpu("g6e.12xlarge", 48, 384, "L40S", 4, [4]int64{10492640, 0, 10492640, 10492640}),
		gpu("g6e.24xlarge", 96, 768, "L40S", 4, [4]int64{15065590, 0, 15065590, 15065590}),
		gpu("g6e.48xlarge", 192, 1536, "L40S", 8, [4]int64{30131180, 0, 30131180, 30131180}),
		gpu("p4d.24xlarge", 96, 1152, "A100-40", 8, [4]int64{21957640, 0, 21957642, 21957640}),
		gpu("p4de.24xlarge", 96, 1152, "A100-80", 8, [4]int64{0, 0, 27447050, 27447050}),
		gpu("p5.4xlarge", 16, 256, "H100", 1, [4]int64{6880000, 0, 6880000, 6880000}),
		gpu("p5.48xlarge", 192, 2048, "H100", 8, [4]int64{55040000, 68800000, 55040000, 55040000}),
		gpu("p5en.48xlarge", 192, 2048, "H200", 8, [4]int64{63296000, 79120000, 63296000, 63296000}),
	}
}

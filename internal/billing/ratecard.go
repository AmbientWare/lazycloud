package billing

import (
	"fmt"
	"slices"
	"time"
)

// Money is an integer count of nanodollars, as the rate card publishes it.
const (
	NanosPerUSD  = int64(1_000_000_000)
	nanosPerCent = NanosPerUSD / 100
	// Currency is the only currency accounts are billed in.
	Currency = "USD"
)

// Credit terms.
const (
	// TrialNanos is the one-time trial credit of a new account.
	TrialNanos = 2 * NanosPerUSD
	// TrialDays is how long the trial credit lasts.
	TrialDays = 30
	// MinPurchaseCents and MaxPurchaseCents bound one credit purchase or
	// automatic reload.
	MinPurchaseCents = 500
	MaxPurchaseCents = 100_000
	// managementFeePercent is the connected-cloud compute fee as a share of
	// the platform fleet's rate.
	managementFeePercent = 8
	// storageMonthSeconds is the 30-day month storage is priced per.
	storageMonthSeconds = 2_592_000
)

// Account limits without a saved card: lower concurrency and only the
// trial GPU models.
const (
	noCardMaxCPUContainers = 10
	noCardMaxGPUs          = 1
)

// PlanID names a plan.
type PlanID string

// Plans, cheapest first.
const (
	PlanFree     PlanID = "free"
	PlanTeam     PlanID = "team"
	PlanBusiness PlanID = "business"
)

// TermsVersion names the published terms of a plan. Only the current terms
// exist.
type TermsVersion string

// The current terms of each plan.
const (
	TermsFree     TermsVersion = "free-v2"
	TermsTeam     TermsVersion = "team-v3"
	TermsBusiness TermsVersion = "business-v2"
)

// GPUType names a GPU model; the empty value is no GPU.
type GPUType string

// The GPU models the platform rents, in published order.
const (
	GPUT4     GPUType = "T4"
	GPUA10G   GPUType = "A10G"
	GPUL4     GPUType = "L4"
	GPUL40S   GPUType = "L40S"
	GPUA10040 GPUType = "A100-40"
	GPUA10080 GPUType = "A100-80"
	GPUH100   GPUType = "H100"
	GPUH200   GPUType = "H200"
	noGPU     GPUType = ""
)

func gpuModels() []GPUType {
	return []GPUType{GPUT4, GPUA10G, GPUL4, GPUL40S, GPUA10040, GPUA10080, GPUH100, GPUH200}
}

// noCardGPUs are the models an account without a saved card may use.
func noCardGPUs() []GPUType { return []GPUType{GPUA10G, GPUL4, GPUT4} }

// BillingOwner is who pays for the machine a container ran on.
type BillingOwner string

const (
	// OwnerPlatformFleet is capacity the platform buys and resells at
	// catalog rates.
	OwnerPlatformFleet BillingOwner = "platform_fleet"
	// OwnerConnectedCloud is a customer's own cloud account; the platform
	// charges a management fee on the fleet rate.
	OwnerConnectedCloud BillingOwner = "connected_cloud"
	// OwnerSelfHosted is hardware a customer joined; it prices at a
	// published zero.
	OwnerSelfHosted BillingOwner = "self_hosted"
)

func billingOwners() []BillingOwner {
	return []BillingOwner{OwnerPlatformFleet, OwnerConnectedCloud, OwnerSelfHosted}
}

// RateClass is a placement choice that multiplies the fleet's compute rate.
type RateClass string

const (
	ClassAuto                 RateClass = "auto"
	ClassPinned               RateClass = "pinned"
	ClassNonPreemptible       RateClass = "non_preemptible"
	ClassPinnedNonPreemptible RateClass = "pinned_non_preemptible"
)

// RateClassFor is the class of a placement: a pinned region or zone and
// preemptible capacity.
func RateClassFor(pinned, preemptible bool) RateClass {
	switch {
	case preemptible && pinned:
		return ClassPinned
	case preemptible:
		return ClassAuto
	case pinned:
		return ClassPinnedNonPreemptible
	default:
		return ClassNonPreemptible
	}
}

// Limit is a count limit that may be unlimited.
type Limit struct {
	Max       int
	Unlimited bool
}

func limitOf(n int) Limit { return Limit{Max: n} }

// Entitlements are the limits and capabilities an account is held to.
// Concurrency is two pools: a container without a GPU counts against CPU
// containers, one with GPUs against GPUs by its card count.
type Entitlements struct {
	MaxCPUContainers int
	MaxGPUs          int
	// GPUTypes are the models the account may ask for.
	GPUTypes            []GPUType
	MaxWorkspaces       Limit
	MaxMembers          Limit
	ConnectedCloud      bool
	CustomDomains       bool
	SelfHosted          bool
	RegionSelection     bool
	RetentionDays       int
	MaxWorkspaceDiskGiB int
}

// Plan is one published plan.
type Plan struct {
	ID            PlanID
	Name          string
	Summary       string
	Terms         TermsVersion
	MonthlyNanos  int64
	IncludedNanos int64
	Entitlements  Entitlements
	// Notes are the plan-specific promises the catalog lists.
	Notes []string
}

// Plans are every plan an account can be on, cheapest first, on their
// current terms.
func Plans() []Plan {
	paidDiskGiB := 1_024
	return []Plan{
		{
			ID: PlanFree, Name: "Free", Summary: "What an account costs before it has agreed to anything.",
			Terms: TermsFree, MonthlyNanos: 0, IncludedNanos: 0,
			Entitlements: Entitlements{
				MaxCPUContainers: 30, MaxGPUs: 5, GPUTypes: gpuModels(),
				MaxWorkspaces: limitOf(1), MaxMembers: limitOf(1),
				SelfHosted: true, RetentionDays: 1,
			},
			Notes: []string{
				"Every workload the platform runs: applications, APIs, functions, jobs, queues, schedules, and sandboxes.",
				"No subscription to cancel and no minimum term.",
			},
		},
		{
			ID: PlanTeam, Name: "Team", Summary: "A monthly subscription with included usage credit.",
			Terms: TermsTeam, MonthlyNanos: 49 * NanosPerUSD, IncludedNanos: 25 * NanosPerUSD,
			Entitlements: Entitlements{
				MaxCPUContainers: 1_000, MaxGPUs: 50, GPUTypes: gpuModels(),
				MaxWorkspaces: Limit{Unlimited: true}, MaxMembers: limitOf(3),
				CustomDomains: true, SelfHosted: true, RegionSelection: true,
				RetentionDays: 30, MaxWorkspaceDiskGiB: paidDiskGiB,
			},
			Notes: []string{
				"The same workloads at the same metered rates, with higher account limits.",
				"Every GPU model the platform rents, with unlimited workspaces.",
				"The member limit includes you and is shared across your workspaces.",
				"One account and invoice for every workspace it owns.",
			},
		},
		{
			ID: PlanBusiness, Name: "Business", Summary: "BYO cloud, unlimited members, and higher concurrency.",
			Terms: TermsBusiness, MonthlyNanos: 199 * NanosPerUSD, IncludedNanos: 100 * NanosPerUSD,
			Entitlements: Entitlements{
				MaxCPUContainers: 2_000, MaxGPUs: 100, GPUTypes: gpuModels(),
				MaxWorkspaces: Limit{Unlimited: true}, MaxMembers: Limit{Unlimited: true},
				ConnectedCloud: true, CustomDomains: true, SelfHosted: true, RegionSelection: true,
				RetentionDays: 90, MaxWorkspaceDiskGiB: paidDiskGiB,
			},
			Notes: []string{
				"Connect your own AWS account, with unlimited members.",
				"Higher concurrency and longer log and artifact retention.",
			},
		},
	}
}

// PlanFor returns the plan with id.
func PlanFor(id PlanID) (Plan, error) {
	for _, plan := range Plans() {
		if plan.ID == id {
			return plan, nil
		}
	}
	return Plan{}, fmt.Errorf("no published plan %q", id)
}

// planOfTerms returns the plan whose current terms are v.
func planOfTerms(v TermsVersion) (Plan, error) {
	for _, plan := range Plans() {
		if plan.Terms == v {
			return plan, nil
		}
	}
	return Plan{}, fmt.Errorf("no published terms %q", v)
}

// accountEntitlements are what an account on plan is held to. A saved card
// unlocks the plan's concurrency and GPU models but adds no credit; a
// complimentary account gets Business.
func accountEntitlements(plan Plan, hasCard, complimentary bool) (Entitlements, error) {
	if complimentary {
		business, err := PlanFor(PlanBusiness)
		if err != nil {
			return Entitlements{}, err
		}
		return business.Entitlements, nil
	}
	e := plan.Entitlements
	if !hasCard {
		e.MaxCPUContainers = noCardMaxCPUContainers
		e.MaxGPUs = noCardMaxGPUs
		e.GPUTypes = noCardGPUs()
	}
	return e, nil
}

// ComputeRate is what one kind of container costs an hour, in nanodollars,
// before the resources it holds are counted.
type ComputeRate struct {
	Owner         BillingOwner
	Class         RateClass
	GPU           GPUType
	ContainerHour int64
	CPUCoreHour   int64
	MemoryGiBHour int64
	GPUCardHour   int64
}

type rateKey struct {
	owner BillingOwner
	class RateClass
	gpu   GPUType
}

func (r ComputeRate) key() rateKey { return rateKey{r.Owner, r.Class, r.GPU} }

// PlatformRate prices egress and volume storage.
type PlatformRate struct {
	EgressGiB      int64
	VolumeGiBMonth int64
}

// DiskRate prices a disk's stored bytes and its attached capacity.
type DiskRate struct {
	StoredGiBMonth   int64
	AttachedGiBMonth int64
}

// rateChange is one reviewed publication. It changes only the compute rates
// it lists, and the platform and disk rates when set.
type rateChange struct {
	version     string
	effectiveAt time.Time
	compute     []ComputeRate
	platform    *PlatformRate
	disk        *DiskRate
}

// shapeRate is what a container on one kind of capacity costs before its
// GPU; gpuRate is one model's fleet rate.
type shapeRate struct {
	owner                                 BillingOwner
	containerHour, cpuCoreHour, memoryGiB int64
}

type gpuRate struct {
	model     GPUType
	fleetHour int64
}

// managementFee is the connected-cloud share of a fleet rate, rounded down
// to whole nanodollars per second.
func managementFee(fleetHour int64) int64 {
	fee := fleetHour * managementFeePercent / 100
	return fee / 3600 * 3600
}

func cardRate(g gpuRate, owner BillingOwner) int64 {
	switch owner {
	case OwnerPlatformFleet:
		return g.fleetHour
	case OwnerConnectedCloud:
		return managementFee(g.fleetHour)
	case OwnerSelfHosted:
		return 0
	}
	panic(fmt.Sprintf("billing owner %q has no GPU rate", owner))
}

func shapesFrom(fleet shapeRate) []shapeRate {
	return []shapeRate{
		fleet,
		{OwnerConnectedCloud, managementFee(fleet.containerHour), managementFee(fleet.cpuCoreHour), managementFee(fleet.memoryGiB)},
		{OwnerSelfHosted, 0, 0, 0},
	}
}

// computeRates is one automatic-placement rate per owner for CPU-only work
// and per owner and GPU model.
func computeRates(shapes []shapeRate, gpus []gpuRate) []ComputeRate {
	var out []ComputeRate
	for _, s := range shapes {
		base := ComputeRate{Owner: s.owner, Class: ClassAuto, ContainerHour: s.containerHour, CPUCoreHour: s.cpuCoreHour, MemoryGiBHour: s.memoryGiB}
		out = append(out, base)
		for _, g := range gpus {
			r := base
			r.GPU, r.GPUCardHour = g.model, cardRate(g, s.owner)
			out = append(out, r)
		}
	}
	return out
}

// Placement describes one rate class and its multipliers over automatic
// placement. Multipliers apply to the platform fleet only.
type Placement struct {
	Class       RateClass
	Name        string
	Pinned      bool
	Preemptible bool
	// CPUMemory and GPU are multipliers in tenths: 15 is 1.5×.
	CPUMemoryTenths int64
	GPUTenths       int64
}

func placements() []Placement {
	out := make([]Placement, 0, 4)
	for _, p := range []struct {
		pinned, preemptible bool
		name                string
	}{
		{false, true, "Automatic"},
		{true, true, "Selected location"},
		{false, false, "Automatic, non-preemptible"},
		{true, false, "Selected location, non-preemptible"},
	} {
		location := int64(10)
		if p.pinned {
			location = 15
		}
		cpuMemory := location
		if !p.preemptible {
			cpuMemory = location * 3
		}
		out = append(out, Placement{
			Class: RateClassFor(p.pinned, p.preemptible), Name: p.name, Pinned: p.pinned, Preemptible: p.preemptible,
			CPUMemoryTenths: cpuMemory, GPUTenths: location,
		})
	}
	return out
}

func multiplied(hourly, tenths int64) int64 {
	if hourly*tenths%10 != 0 {
		panic(fmt.Sprintf("rate %d × %d/10 is not a whole number of nanodollars", hourly, tenths))
	}
	return hourly * tenths / 10
}

// placementRates applies every placement's multipliers to the automatic
// rates. keep selects the classes to publish.
func placementRates(auto []ComputeRate, keep func(RateClass) bool) []ComputeRate {
	var out []ComputeRate
	for _, p := range placements() {
		if !keep(p.Class) {
			continue
		}
		for _, r := range auto {
			r.Class = p.Class
			if r.Owner == OwnerPlatformFleet {
				r.CPUCoreHour = multiplied(r.CPUCoreHour, p.CPUMemoryTenths)
				r.MemoryGiBHour = multiplied(r.MemoryGiBHour, p.CPUMemoryTenths)
				r.GPUCardHour = multiplied(r.GPUCardHour, p.GPUTenths)
			}
			out = append(out, r)
		}
	}
	return out
}

// rateHistory is the reviewed price history of the rate card.
// Publications keep their original figures and dates; later ones override
// earlier ones key by key.
func rateHistory() []rateChange {
	initialGPUs := []gpuRate{
		{GPUT4, 560_880_000}, {GPUA10G, 1_201_201_200}, {GPUL4, 899_398_800}, {GPUL40S, 2_138_346_000},
		{GPUA10040, 1_993_860_000}, {GPUA10080, 2_925_626_400}, {GPUH100, 3_372_120_000}, {GPUH200, 3_918_236_400},
	}
	septemberGPUs := slices.Clone(initialGPUs)
	for n, g := range septemberGPUs {
		switch g.model {
		case GPUT4:
			septemberGPUs[n].fleetHour = 550_000_000
		case GPUA10G:
			septemberGPUs[n].fleetHour = 1_000_000_000
		case GPUL4:
			septemberGPUs[n].fleetHour = 750_000_000
		case GPUL40S, GPUA10040, GPUA10080, GPUH100, GPUH200, noGPU, GPUAny:
		}
	}
	spotGPUs := []gpuRate{
		{GPUT4, 350_000_000}, {GPUA10G, 750_000_000}, {GPUL4, 500_000_000}, {GPUL40S, 1_350_000_000},
		{GPUA10040, 1_500_000_000}, {GPUA10080, 2_925_626_400}, {GPUH100, 2_250_000_000}, {GPUH200, 3_250_000_000},
	}
	initial := computeRates(shapesFrom(shapeRate{OwnerPlatformFleet, 0, 55_126_800, 7_560_000}), initialGPUs)
	september := shapesFrom(shapeRate{OwnerPlatformFleet, 0, 22_000_000, 7_500_000})
	all := func(RateClass) bool { return true }
	gpuOnly := func(rates []ComputeRate) []ComputeRate {
		return slices.DeleteFunc(rates, func(r ComputeRate) bool { return r.GPU == noGPU })
	}
	return []rateChange{
		{
			version: "2026-08-18.a", effectiveAt: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
			compute: initial, platform: &PlatformRate{EgressGiB: 0, VolumeGiBMonth: 50_000_000},
		},
		{
			version: "2026-09-09.a", effectiveAt: time.Date(2026, 9, 9, 0, 0, 0, 0, time.UTC),
			compute: placementRates(initial, func(c RateClass) bool { return c != ClassAuto }),
		},
		{
			version: "2026-09-10.a", effectiveAt: time.Date(2026, 9, 10, 4, 9, 5, 835918000, time.UTC),
			compute:  placementRates(computeRates(september, septemberGPUs), all),
			platform: &PlatformRate{EgressGiB: 130_000_000, VolumeGiBMonth: 50_000_000},
		},
		{
			version: "2026-09-10.b", effectiveAt: time.Date(2026, 9, 10, 4, 58, 18, 736793000, time.UTC),
			compute: gpuOnly(placementRates(computeRates(september, spotGPUs), all)),
		},
		{
			version: "2026-09-23.a", effectiveAt: time.Date(2026, 9, 23, 0, 0, 0, 0, time.UTC),
			disk: &DiskRate{StoredGiBMonth: 50_000_000, AttachedGiBMonth: 200_000_000},
		},
	}
}

// RateCard is the rates in force at one instant.
type RateCard struct {
	Version     string
	EffectiveAt time.Time
	compute     map[rateKey]ComputeRate
	// classSince is when each rate class was last republished.
	classSince map[RateClass]time.Time
	Platform   PlatformRate
	Disk       *DiskRate
}

// rates holds the published history, built once by the owner.
type rates struct {
	history []rateChange
}

func newRates() rates { return rates{history: rateHistory()} }

// cardAt merges every publication in force at t.
func (r rates) cardAt(t time.Time) (RateCard, error) {
	card := RateCard{compute: map[rateKey]ComputeRate{}, classSince: map[RateClass]time.Time{}}
	found := false
	for _, change := range r.history {
		if change.effectiveAt.After(t) {
			break
		}
		found = true
		card.Version, card.EffectiveAt = change.version, change.effectiveAt
		for _, rate := range change.compute {
			card.compute[rate.key()] = rate
			card.classSince[rate.Class] = change.effectiveAt
		}
		if change.platform != nil {
			card.Platform = *change.platform
		}
		if change.disk != nil {
			disk := *change.disk
			card.Disk = &disk
		}
	}
	if !found {
		return RateCard{}, fmt.Errorf("no published rates cover %s", t.Format(time.RFC3339))
	}
	return card, nil
}

// changesBetween returns the publication instants strictly inside (from, to),
// where an interval must be split so each part prices under one card.
func (r rates) changesBetween(from, to time.Time) []time.Time {
	var out []time.Time
	for _, change := range r.history {
		if change.effectiveAt.After(from) && change.effectiveAt.Before(to) {
			out = append(out, change.effectiveAt)
		}
	}
	return out
}

// compute returns the rate for one kind of container.
func (c RateCard) computeRate(owner BillingOwner, class RateClass, gpu GPUType) (ComputeRate, error) {
	rate, ok := c.compute[rateKey{owner, class, gpu}]
	if !ok {
		return ComputeRate{}, fmt.Errorf("no published %s rate for %s capacity with GPU %q", class, owner, gpu)
	}
	return rate, nil
}

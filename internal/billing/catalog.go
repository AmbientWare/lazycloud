package billing

import (
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// Catalog is the public pricing catalog in force at t: plans, the limits
// without a saved card, and the metered rates with their placement
// multipliers.
func (b *Billing) Catalog(t time.Time) (apitypes.PricingCatalog, error) {
	card, err := b.rates.cardAt(t)
	if err != nil {
		return apitypes.PricingCatalog{}, &InvalidError{Message: err.Error()}
	}
	out := apitypes.PricingCatalog{
		Trial:                              apitypes.TrialTerms{AmountNanos: TrialNanos, DurationDays: TrialDays, OneTime: true},
		PricingVersion:                     card.Version,
		MeteredRatesEffectiveAt:            card.EffectiveAt,
		Currency:                           Currency,
		ConnectedCloudManagementFeePercent: managementFeePercent,
		NoPaymentMethod: apitypes.NoPaymentMethodTerms{
			MaxConcurrentCpuContainers: noCardMaxCPUContainers, MaxConcurrentGpus: noCardMaxGPUs,
			GpuTypes: gpuNames(noCardGPUs()),
		},
		PlatformRate: apitypes.PlatformRate{
			NanosPerEgressGib: card.Platform.EgressGiB, NanosPerVolumeGibMonth: card.Platform.VolumeGiBMonth,
			StorageMonthSeconds: storageMonthSeconds,
		},
		CreditPurchase: apitypes.CreditPurchaseTerms{MinimumCents: MinPurchaseCents, MaximumCents: MaxPurchaseCents},
	}
	if card.Disk != nil {
		out.DiskRate = &apitypes.DiskRate{NanosPerStoredGibMonth: card.Disk.StoredGiBMonth, NanosPerAttachedGibMonth: card.Disk.AttachedGiBMonth}
	}
	for _, plan := range Plans() {
		out.Plans = append(out.Plans, apitypes.PublishedPlan{
			Id: apitypes.PlanId(plan.ID), TermsVersion: apitypes.TermsVersion(plan.Terms), Name: plan.Name,
			Summary: plan.Summary, MonthlyNanos: plan.MonthlyNanos, IncludedNanos: plan.IncludedNanos,
			Entitlements: entitlementsOut(plan.Entitlements), Terms: plan.Notes,
		})
	}
	for _, owner := range billingOwners() {
		rate, err := card.computeRate(owner, ClassAuto, noGPU)
		if err != nil {
			return apitypes.PricingCatalog{}, err
		}
		out.ShapeRates = append(out.ShapeRates, apitypes.ShapeRate{
			BillingOwner: apitypes.BillingOwner(owner), NanosPerContainerHour: rate.ContainerHour,
			NanosPerCpuCoreHour: rate.CPUCoreHour, NanosPerMemoryGibHour: rate.MemoryGiBHour,
		})
	}
	for _, model := range GPUModels() {
		var cards apitypes.CardRates
		for _, owner := range billingOwners() {
			rate, err := card.computeRate(owner, ClassAuto, model.Type)
			if err != nil {
				return apitypes.PricingCatalog{}, err
			}
			switch owner {
			case OwnerPlatformFleet:
				cards.PlatformFleet = rate.GPUCardHour
			case OwnerConnectedCloud:
				cards.ConnectedCloud = rate.GPUCardHour
			case OwnerSelfHosted:
				cards.SelfHosted = rate.GPUCardHour
			}
		}
		out.GpuRates = append(out.GpuRates, apitypes.GpuRate{GpuType: string(model.Type), Enabled: model.Enabled, NanosPerCardHour: cards})
	}
	for _, placement := range placements() {
		since, ok := card.classSince[placement.Class]
		if !ok {
			continue
		}
		published := apitypes.PlacementRate{
			RateClass: apitypes.RateClass(placement.Class), EffectiveAt: since, Pinned: placement.Pinned,
			Preemptible: placement.Preemptible, Name: placement.Name,
			CpuMemoryMultiplier: float32(placement.CPUMemoryTenths) / 10, GpuMultiplier: float32(placement.GPUTenths) / 10,
		}
		for _, owner := range billingOwners() {
			for _, model := range append([]GPUType{noGPU}, gpuModels()...) {
				rate, err := card.computeRate(owner, placement.Class, model)
				if err != nil {
					return apitypes.PricingCatalog{}, err
				}
				row := apitypes.ComputeRate{
					BillingOwner: apitypes.BillingOwner(owner), NanosPerContainerHour: rate.ContainerHour,
					NanosPerCpuCoreHour: rate.CPUCoreHour, NanosPerMemoryGibHour: rate.MemoryGiBHour,
					NanosPerGpuCardHour: rate.GPUCardHour,
				}
				if model != noGPU {
					name := string(model)
					row.GpuType = &name
				}
				published.ComputeRates = append(published.ComputeRates, row)
			}
		}
		out.PlacementRates = append(out.PlacementRates, published)
	}
	return out, nil
}

func gpuNames(models []GPUType) []string {
	out := make([]string, len(models))
	for n, m := range models {
		out[n] = string(m)
	}
	return out
}

func entitlementsOut(e Entitlements) apitypes.PlanEntitlements {
	out := apitypes.PlanEntitlements{
		MaxConcurrentCpuContainers: e.MaxCPUContainers, MaxConcurrentGpus: e.MaxGPUs, GpuTypes: gpuNames(e.GPUTypes),
		ConnectedCloud: e.ConnectedCloud, CustomDomains: e.CustomDomains, SelfHosted: e.SelfHosted,
		RetentionDays: e.RetentionDays, RegionSelection: e.RegionSelection, MaxWorkspaceDiskGib: e.MaxWorkspaceDiskGiB,
	}
	if !e.MaxWorkspaces.Unlimited {
		n := e.MaxWorkspaces.Max
		out.MaxWorkspaces = &n
	}
	if !e.MaxMembers.Unlimited {
		n := e.MaxMembers.Max
		out.MaxMembers = &n
	}
	return out
}

import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type { GpuTypeEntitlement, PlanEntitlements, PricingCatalog } from "@/lib/api/schemas";

export function pricingCatalogQueryOptions() {
  return queryOptions({
    queryKey: ["pricing-catalog"] as const,
    queryFn: async () => toPricingCatalog(await ok(api.GET("/v1/pricing"))),
    staleTime: 5 * 60_000,
  });
}

/** The catalog in the shape the pricing page and plan dialog read. */
function toPricingCatalog(catalog: Schemas["PricingCatalog"]): PricingCatalog {
  const offered = catalog.gpu_rates.map((rate) => rate.gpu_type);
  return {
    trial: catalog.trial,
    pricing_version: catalog.pricing_version,
    metered_rates_effective_at: catalog.metered_rates_effective_at,
    currency: catalog.currency,
    connected_cloud_management_fee_percent: catalog.connected_cloud_management_fee_percent,
    credit_purchase: catalog.credit_purchase,
    no_payment_method: catalog.no_payment_method,
    plans: catalog.plans.map((plan) => ({
      ...plan,
      entitlements: toPlanEntitlements(plan.entitlements, offered),
    })),
    shape_rates: catalog.shape_rates,
    gpu_rates: catalog.gpu_rates,
    placement_rates: catalog.placement_rates.map((placement) => ({
      ...placement,
      cpu_memory_multiplier: String(placement.cpu_memory_multiplier),
      gpu_multiplier: String(placement.gpu_multiplier),
      compute_rates: placement.compute_rates.map((rate) => ({
        ...rate,
        gpu_type: rate.gpu_type ?? "",
      })),
    })),
    platform_rate: catalog.platform_rate,
    disk_rate: catalog.disk_rate ?? null,
  };
}

/**
 * Plan limits as the dashboard words them: an absent ceiling is unlimited, and a
 * plan naming every GPU model the catalog rents offers all of them.
 */
export function toPlanEntitlements(
  entitlements: Schemas["PlanEntitlements"],
  offeredGpuTypes?: readonly string[],
): PlanEntitlements {
  return {
    ...entitlements,
    max_workspaces: entitlements.max_workspaces ?? "unlimited",
    max_members: entitlements.max_members ?? "unlimited",
    gpu_types: gpuTypes(entitlements.gpu_types, offeredGpuTypes),
  };
}

function gpuTypes(granted: string[], offered: readonly string[] | undefined): GpuTypeEntitlement {
  if (offered?.length && offered.every((model) => granted.includes(model))) return "all";
  return granted;
}

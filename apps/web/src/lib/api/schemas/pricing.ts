import { z } from "zod";

export const billingPlanIdSchema = z.enum(["free", "team"]);
export type BillingPlanId = z.infer<typeof billingPlanIdSchema>;

export const entitlementLimitSchema = z.union([
  z.number().int().positive(),
  z.literal("unlimited"),
]);

export const planEntitlementsSchema = z
  .object({
    max_apps: z.number().int().positive(),
    max_concurrent_containers: z.number().int().positive(),
    max_members: entitlementLimitSchema,
    connected_cloud: z.boolean(),
    custom_domains: z.boolean(),
    self_hosted: z.boolean(),
  })
  .strict();
export type PlanEntitlements = z.infer<typeof planEntitlementsSchema>;

export const publishedPlanSchema = z
  .object({
    id: billingPlanIdSchema,
    name: z.string(),
    summary: z.string(),
    monthly_nanos: z.number().int().nonnegative(),
    included_nanos: z.number().int().nonnegative(),
    entitlements: planEntitlementsSchema,
    terms: z.array(z.string()),
  })
  .strict();
export type PublishedPlan = z.infer<typeof publishedPlanSchema>;

const billingOwnerSchema = z.enum(["platform_fleet", "connected_cloud", "self_hosted"]);

const publishedShapeRateSchema = z
  .object({
    billing_owner: billingOwnerSchema,
    nanos_per_container_hour: z.number().int().nonnegative(),
    nanos_per_cpu_core_hour: z.number().int().nonnegative(),
    nanos_per_memory_gib_hour: z.number().int().nonnegative(),
  })
  .strict();

const ownerRatesSchema = z
  .object({
    platform_fleet: z.number().int().nonnegative(),
    connected_cloud: z.number().int().nonnegative(),
    self_hosted: z.number().int().nonnegative(),
  })
  .strict();

const publishedGpuRateSchema = z
  .object({
    gpu_type: z.string(),
    nanos_per_card_hour: ownerRatesSchema,
  })
  .strict();

export const pricingCatalogSchema = z
  .object({
    pricing_version: z.string(),
    currency: z.string().regex(/^[A-Z]{3}$/),
    connected_cloud_management_fee_percent: z.number().int().min(0).max(100),
    no_payment_method: z
      .object({
        included_nanos: z.number().int().nonnegative(),
        max_concurrent_containers: z.number().int().positive(),
      })
      .strict(),
    plans: z.array(publishedPlanSchema),
    shape_rates: z.array(publishedShapeRateSchema),
    gpu_rates: z.array(publishedGpuRateSchema),
    platform_rate: z
      .object({
        nanos_per_egress_gib: z.number().int().nonnegative(),
        nanos_per_volume_gib_month: z.number().int().nonnegative(),
        storage_month_seconds: z.number().int().positive(),
      })
      .strict(),
  })
  .strict();
export type PricingCatalog = z.infer<typeof pricingCatalogSchema>;

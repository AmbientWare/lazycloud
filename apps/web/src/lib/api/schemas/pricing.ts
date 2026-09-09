import { z } from "zod";

export const billingPlanIdSchema = z.enum(["free", "team", "business"]);
export type BillingPlanId = z.infer<typeof billingPlanIdSchema>;
export const billingTermsVersionSchema = z.enum([
  "free-v1",
  "team-v1",
  "free-v2",
  "team-v2",
  "business-v1",
]);
export type BillingTermsVersion = z.infer<typeof billingTermsVersionSchema>;

export const entitlementLimitSchema = z.union([
  z.number().int().positive(),
  z.literal("unlimited"),
]);

/**
 * Which cards a plan may ask for: the models it names, or every model the
 * platform rents.
 *
 * Open strings rather than an enum, like the rate card's own `gpu_type`. The
 * schedulable list is the server's to publish, and a model added there must
 * reach the page that lists it without this file being edited.
 */
export const gpuTypeEntitlementSchema = z.union([z.literal("all"), z.array(z.string()).min(1)]);
export type GpuTypeEntitlement = z.infer<typeof gpuTypeEntitlementSchema>;

export const planEntitlementsSchema = z
  .object({
    // Two pools rather than one count with a GPU share inside it: a container
    // counts against one of them, never both, so GPU work cannot crowd out the
    // account's web apps.
    max_concurrent_cpu_containers: z.number().int().positive(),
    max_concurrent_gpus: z.number().int().positive(),
    gpu_types: gpuTypeEntitlementSchema,
    max_workspaces: entitlementLimitSchema,
    max_members: entitlementLimitSchema,
    connected_cloud: z.boolean(),
    custom_domains: z.boolean(),
    self_hosted: z.boolean(),
    log_retention_days: z.number().int().positive(),
    region_selection: z.boolean(),
  })
  .strict();
export type PlanEntitlements = z.infer<typeof planEntitlementsSchema>;

export const publishedPlanSchema = z
  .object({
    id: billingPlanIdSchema,
    terms_version: billingTermsVersionSchema,
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

export const publishedPlacementRateSchema = z
  .object({
    rate_class: z
      .string()
      .min(1)
      .max(64)
      .regex(/^[a-z][a-z0-9_-]*$/),
    effective_at: z.string().datetime({ offset: true }),
    pinned: z.boolean(),
    preemptible: z.boolean(),
    name: z.string(),
    cpu_memory_multiplier: z
      .string()
      .regex(/^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/)
      .refine((value) => Number.isFinite(Number(value)) && Number(value) > 0),
    gpu_multiplier: z
      .string()
      .regex(/^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/)
      .refine((value) => Number.isFinite(Number(value)) && Number(value) > 0),
    compute_rates: z.array(
      z
        .object({
          billing_owner: billingOwnerSchema,
          gpu_type: z.string(),
          nanos_per_container_hour: z.number().int().nonnegative(),
          nanos_per_cpu_core_hour: z.number().int().nonnegative(),
          nanos_per_memory_gib_hour: z.number().int().nonnegative(),
          nanos_per_gpu_card_hour: z.number().int().nonnegative(),
        })
        .strict(),
    ),
  })
  .strict();
export type PublishedPlacementRate = z.infer<typeof publishedPlacementRateSchema>;

export const pricingCatalogSchema = z
  .object({
    trial: z
      .object({
        amount_nanos: z.number().int().positive(),
        duration_days: z.number().int().positive(),
        one_time: z.literal(true),
      })
      .strict(),
    pricing_version: z.string(),
    metered_rates_effective_at: z.string().datetime({ offset: true }),
    currency: z.string().regex(/^[A-Z]{3}$/),
    connected_cloud_management_fee_percent: z.number().int().min(0).max(100),
    credit_purchase: z
      .object({
        minimum_cents: z.number().int().positive(),
        maximum_cents: z.number().int().positive(),
      })
      .strict(),
    no_payment_method: z
      .object({
        max_concurrent_cpu_containers: z.number().int().positive(),
        max_concurrent_gpus: z.number().int().positive(),
      })
      .strict(),
    plans: z.array(publishedPlanSchema),
    shape_rates: z.array(publishedShapeRateSchema),
    gpu_rates: z.array(publishedGpuRateSchema),
    placement_rates: z.array(publishedPlacementRateSchema),
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

/* Generated from `shared.billing_rate_card`. Do not edit.

   Regenerate with:
     uv run lazycloud-admin billing write-pricing-catalog --output apps/web/src/routes/-marketing/pricingCatalog.ts

   That card is also what `lazycloud-admin billing publish-rates` writes into the
   rate tables the pricer quotes from, so the figures below and the figures a
   customer is charged have one owner. The pricing page renders without calling
   the API, which is why they are compiled in rather than fetched, and
   `tests/contracts` fails while this file and the card disagree — an edit made
   here instead of there is lost at the next regeneration and would have quoted a
   price the platform never held.

   Figures are nanodollars — billionths of a dollar — per hour of a whole unit: a
   container, a core, a gibibyte, a card. They are exact, so the page renders a
   published rate rather than a rounded one. */

export const BILLING_CURRENCY = "USD";

export const RATES_EFFECTIVE_ON = "13 August 2026";

export const billingOwners = ["platform_fleet", "connected_cloud", "self_hosted"] as const;
export type BillingOwner = (typeof billingOwners)[number];

export type PublishedShapeRate = {
  /** What a container costs before any of its resources are counted. */
  nanosPerContainerHour: number;
  nanosPerCpuCoreHour: number;
  nanosPerMemoryGibHour: number;
};

/* Every container on one kind of capacity pays these three, whatever card sits
   beside it. Only the card's own rate varies, which is why the card is two
   small tables rather than one row per pair. */
export const publishedShapeRates = {
  platform_fleet: {
    nanosPerContainerHour: 0,
    nanosPerCpuCoreHour: 55_126_800,
    nanosPerMemoryGibHour: 7_560_000,
  },
  connected_cloud: {
    nanosPerContainerHour: 0,
    nanosPerCpuCoreHour: 2_854_800,
    nanosPerMemoryGibHour: 273_600,
  },
  self_hosted: {
    nanosPerContainerHour: 0,
    nanosPerCpuCoreHour: 0,
    nanosPerMemoryGibHour: 0,
  },
} as const satisfies Record<BillingOwner, PublishedShapeRate>;

export type PublishedGpuRate = {
  gpuType: string;
  platformFleetNanosPerCardHour: number;
  connectedCloudNanosPerCardHour: number;
};

/* Cheapest first on LazyCloud capacity. The two columns rank differently, so
   one order has to lead. */
export const publishedGpuRates = [
  {
    gpuType: "T4",
    platformFleetNanosPerCardHour: 560_880_000,
    connectedCloudNanosPerCardHour: 26_280_000,
  },
  {
    gpuType: "L4",
    platformFleetNanosPerCardHour: 899_398_800,
    connectedCloudNanosPerCardHour: 48_585_600,
  },
  {
    gpuType: "A10G",
    platformFleetNanosPerCardHour: 1_201_201_200,
    connectedCloudNanosPerCardHour: 64_681_200,
  },
  {
    gpuType: "A100-40",
    platformFleetNanosPerCardHour: 1_993_860_000,
    connectedCloudNanosPerCardHour: 254_001_600,
  },
  {
    gpuType: "L40S",
    platformFleetNanosPerCardHour: 2_138_346_000,
    connectedCloudNanosPerCardHour: 128_703_600,
  },
  {
    gpuType: "A100-80",
    platformFleetNanosPerCardHour: 2_925_626_400,
    connectedCloudNanosPerCardHour: 335_998_800,
  },
  {
    gpuType: "H100",
    platformFleetNanosPerCardHour: 3_372_120_000,
    connectedCloudNanosPerCardHour: 844_801_200,
  },
  {
    gpuType: "H200",
    platformFleetNanosPerCardHour: 3_918_236_400,
    connectedCloudNanosPerCardHour: 1_000_800_000,
  },
] as const satisfies readonly PublishedGpuRate[];

/* Published at a stated zero rather than left off the page. Both are metered,
   both reach the ledger and the invoice, and both read $0.00 — which is how a
   customer can tell the traffic and the storage are measured and free rather
   than unmeasured. */
export const publishedPlatformRates = {
  nanosPerEgressByte: 0,
  nanosPerVolumeByteSecond: 0,
} as const;

/** What a gibibyte of egress costs, from the per-byte rate exactly. */
export const EGRESS_NANOS_PER_GIB = 0;

/**
 * What a gibibyte kept for a thirty-day month costs, from the per-byte-second
 * rate exactly.
 */
export const VOLUME_STORAGE_NANOS_PER_GIB_MONTH = 0;

export const planIds = ["free", "team"] as const;
export type PlanId = (typeof planIds)[number];

export type PublishedPlan = {
  monthlyNanos: number;
  includedNanos: number;
};

/* What a plan charges and what it comes with. What it is called and how it is
   described belongs to the page, which joins the two on the id. */
export const publishedPlans = {
  free: { monthlyNanos: 0, includedNanos: 5_000_000_000 },
  team: { monthlyNanos: 200_000_000_000, includedNanos: 100_000_000_000 },
} as const satisfies Record<PlanId, PublishedPlan>;

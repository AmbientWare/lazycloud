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

   Figures are nanodollars — billionths of a dollar — per whole unit of whatever
   the line is sold by: an hour of a container, a core, a gibibyte of memory, a
   card; a gibibyte moved; a gibibyte kept for a thirty-day month. Every one is a
   whole number, so the page renders a published price rather than a rounded
   one. */

export type BillingOwner = "platform_fleet" | "connected_cloud" | "self_hosted";

export type PublishedShapeRate = {
  nanosPerCpuCoreHour: number;
  nanosPerMemoryGibHour: number;
};

/* Every container on one kind of capacity pays these three, whatever card sits
   beside it. Only the card's own rate varies, which is why the card is two
   small tables rather than one row per pair. */
export const publishedShapeRates = {
  platform_fleet: {
    nanosPerCpuCoreHour: 55_126_800,
    nanosPerMemoryGibHour: 7_560_000,
  },
  connected_cloud: {
    nanosPerCpuCoreHour: 4_410_000,
    nanosPerMemoryGibHour: 604_800,
  },
  self_hosted: {
    nanosPerCpuCoreHour: 0,
    nanosPerMemoryGibHour: 0,
  },
} as const satisfies Record<BillingOwner, PublishedShapeRate>;

export type PublishedGpuRate = {
  gpuType: string;
  /** What one card of this model costs an hour, on each kind of capacity. */
  nanosPerCardHour: Record<BillingOwner, number>;
};

/* Most expensive first on LazyCloud capacity. The capacities rank differently,
   so one published order has to lead. */
export const publishedGpuRates = [
  {
    gpuType: "H200",
    nanosPerCardHour: {
      platform_fleet: 3_918_236_400,
      connected_cloud: 313_455_600,
      self_hosted: 0,
    },
  },
  {
    gpuType: "H100",
    nanosPerCardHour: {
      platform_fleet: 3_372_120_000,
      connected_cloud: 269_769_600,
      self_hosted: 0,
    },
  },
  {
    gpuType: "A100-80",
    nanosPerCardHour: {
      platform_fleet: 2_925_626_400,
      connected_cloud: 234_046_800,
      self_hosted: 0,
    },
  },
  {
    gpuType: "L40S",
    nanosPerCardHour: {
      platform_fleet: 2_138_346_000,
      connected_cloud: 171_064_800,
      self_hosted: 0,
    },
  },
  {
    gpuType: "A100-40",
    nanosPerCardHour: {
      platform_fleet: 1_993_860_000,
      connected_cloud: 159_508_800,
      self_hosted: 0,
    },
  },
  {
    gpuType: "A10G",
    nanosPerCardHour: {
      platform_fleet: 1_201_201_200,
      connected_cloud: 96_094_800,
      self_hosted: 0,
    },
  },
  {
    gpuType: "L4",
    nanosPerCardHour: {
      platform_fleet: 899_398_800,
      connected_cloud: 71_949_600,
      self_hosted: 0,
    },
  },
  {
    gpuType: "T4",
    nanosPerCardHour: {
      platform_fleet: 560_880_000,
      connected_cloud: 44_870_400,
      self_hosted: 0,
    },
  },
] as const satisfies readonly PublishedGpuRate[];

/**
 * What a gibibyte of traffic leaving the platform costs.
 *
 * A stated zero rather than a figure left off the page: egress is metered, it
 * reaches the ledger and the invoice, and it reads $0.00 — which is how a
 * customer can tell the traffic is measured and free rather than unmeasured.
 */
export const EGRESS_NANOS_PER_GIB = 0;

/**
 * What this platform charges to run a container on capacity somebody else pays for,
 * as a percentage of the same container's price on our own fleet.
 *
 * Compute only. Volumes and egress are this platform's own infrastructure and are
 * charged whole wherever the container ran.
 */
export const CONNECTED_CLOUD_MANAGEMENT_FEE_PERCENT = 8;

/** What a gibibyte kept on a volume for a thirty-day month costs. */
export const VOLUME_STORAGE_NANOS_PER_GIB_MONTH = 50_000_000;

export const planIds = ["free", "team"] as const;
export type PlanId = (typeof planIds)[number];

export type PublishedPlan = {
  name: string;
  summary: string;
  monthlyNanos: number;
  includedNanos: number;
  maxConcurrentContainers: number;
  /** What the plan promises beyond its figures; it never restates one of them. */
  terms: readonly string[];
};

/* Every plan whole: what it is called, what it charges, what it comes with,
   and what it promises. A surface offering a plan renders these and keeps no
   copy of its own, so a plan added to the card is a plan the pricing page and
   the dashboard describe without being edited. */
export const publishedPlans = {
  free: {
    name: "Free",
    summary: "What an account costs before it has agreed to anything.",
    monthlyNanos: 0,
    includedNanos: 5_000_000_000,
    maxConcurrentContainers: 200,
    terms: [
      "Every workload the platform runs: applications, APIs, functions, jobs, queues, schedules, and sandboxes.",
      "No subscription to cancel and no minimum term.",
    ],
  },
  team: {
    name: "Team",
    summary: "A monthly subscription that comes with compute included.",
    monthlyNanos: 200_000_000_000,
    includedNanos: 100_000_000_000,
    maxConcurrentContainers: 1_000,
    terms: [
      "The same workloads at the same rates. A plan changes what you pay, not what you can run.",
      "Room for a team to run more at once on one account and one invoice.",
    ],
  },
} as const satisfies Record<PlanId, PublishedPlan>;

/* What an account gets before a card is on file. Not a plan — every plan
   falls back to these until somebody can be billed. */
export const NO_CARD_INCLUDED_NANOS = 1_000_000_000;
export const NO_CARD_MAX_CONTAINERS = 10;

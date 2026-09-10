import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { createLazyFileRoute } from "@tanstack/react-router";

import type { PricingCatalog, PublishedPlacementRate } from "@/lib/api/schemas";
import { gpuModelsLabel, limitFigure, memberLimitFigure } from "@/lib/entitlements";
import { DOCS_URL } from "@/lib/env";
import { countLabel } from "@/lib/format";
import { exactDollars } from "@/lib/money";
import { pricingCatalogQueryOptions } from "@/lib/queries/pricing";
import { cn } from "@/lib/utils";

import { MarketingLayout } from "./-marketing/MarketingLayout";
import {
  FinalCta,
  Glyph,
  GetStartedButton,
  MarketingButton,
  MarketingCard,
  shell,
} from "./-marketing/MarketingPrimitives";

export const Route = createLazyFileRoute("/pricing")({
  component: MarketingPricing,
});

const SECONDS_PER_HOUR = 3600;

/** Which unit every figure on the page is currently read in. */
type Meter = "hour" | "second";

function metered(nanosPerHour: number, meter: Meter): number | string {
  if (meter === "hour") return nanosPerHour;
  if (nanosPerHour % SECONDS_PER_HOUR === 0) return nanosPerHour / SECONDS_PER_HOUR;
  return `≈ ${new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 12,
  }).format(nanosPerHour / SECONDS_PER_HOUR / 1_000_000_000)}`;
}

const meters = [
  { value: "hour", label: "Per hour" },
  { value: "second", label: "Per second" },
] as const satisfies readonly { value: Meter; label: string }[];

/** One priced line: what it is, what it costs, and the unit that price is in. */
type RateLine = {
  label: string;
  /** A published rate in nanodollars, or a figure that is not money — a share of one. */
  figure: number | string;
  unit: string;
};

type RateGroup = {
  heading?: string;
  lines: readonly RateLine[];
};

function perLabel(meter: Meter): string {
  return meter === "second" ? "sec" : "hr";
}

function computeGroups(placement: PublishedPlacementRate, meter: Meter): readonly RateGroup[] {
  const rates = placement.compute_rates.filter((rate) => rate.billing_owner === "platform_fleet");
  const shape = rates.find((rate) => rate.gpu_type === "");
  if (!shape) throw new Error("the pricing catalog has no platform fleet rate");
  const fleetGpuRates = rates
    .filter((rate) => rate.gpu_type !== "")
    .sort((left, right) => right.nanos_per_gpu_card_hour - left.nanos_per_gpu_card_hour);
  const per = perLabel(meter);
  return [
    {
      heading: "GPU",
      lines: fleetGpuRates.map((rate) => ({
        label: rate.gpu_type,
        figure: metered(rate.nanos_per_gpu_card_hour, meter),
        unit: `/ ${per}`,
      })),
    },
    {
      lines: [
        {
          label: "CPU",
          figure: metered(shape.nanos_per_cpu_core_hour, meter),
          unit: `/ CPU / ${per}`,
        },
      ],
    },
    {
      lines: [
        {
          label: "Memory",
          figure: metered(shape.nanos_per_memory_gib_hour, meter),
          unit: `/ GiB / ${per}`,
        },
      ],
    },
  ];
}

function platformGroups(catalog: PricingCatalog): readonly RateGroup[] {
  return [
    {
      lines: [
        {
          label: "Volumes",
          figure: catalog.platform_rate.nanos_per_volume_gib_month,
          unit: "/ GiB / 30 days",
        },
      ],
    },
    {
      lines: [
        {
          label: "Egress",
          figure: catalog.platform_rate.nanos_per_egress_gib,
          unit: "/ GiB",
        },
      ],
    },
    {
      lines: [
        {
          label: "Bring your own cloud",
          figure: `${catalog.connected_cloud_management_fee_percent}%`,
          unit: "management fee",
        },
      ],
    },
  ];
}

/* The one account-wide fact a reader needs before choosing a plan: what they get
   before they have paid for anything. The rest is disclosure, not pricing. */
function accountTerm(catalog: PricingCatalog): string {
  const terms = catalog.no_payment_method;
  return `New accounts receive a one-time ${exactDollars(catalog.trial.amount_nanos)} usage credit, valid for ${catalog.trial.duration_days} days. Without a saved card, you can run ${countLabel(terms.max_concurrent_cpu_containers, "CPU container")} at once and ${countLabel(terms.max_concurrent_gpus, "GPU card")}. Further usage needs prepaid credit.`;
}

const sectionTitle =
  "font-serif text-[clamp(1.75rem,4.2vw,2.5rem)] leading-[1.05] font-normal tracking-[-0.005em] text-balance [&_em]:text-brand [&_em]:italic";

function MarketingPricing() {
  const [meter, setMeter] = useState<Meter>("hour");
  const fleetRatesId = useId();
  const pricing = useQuery(pricingCatalogQueryOptions());
  const catalog = pricing.data;

  if (!catalog) {
    return (
      <MarketingLayout>
        <main className={cn(shell, "py-24")} id="marketing-main">
          <p className={pricing.error ? "text-destructive" : "text-muted-foreground"}>
            {pricing.error?.message ?? "Loading current pricing…"}
          </p>
        </main>
      </MarketingLayout>
    );
  }

  const placement = catalog.placement_rates.find((rate) => rate.rate_class === "auto");
  if (!placement) throw new Error("the pricing catalog has no base compute rates");

  return (
    <MarketingLayout>
      <main id="marketing-main">
        <section className="border-b border-border bg-background">
          <div
            className={cn(
              shell,
              "grid grid-cols-[minmax(0,0.68fr)_minmax(0,1fr)] gap-x-14 gap-y-12 pt-12 pb-16 sm:pt-14 lg:gap-x-20 lg:pt-20 lg:pb-24 max-lg:grid-cols-1",
            )}
          >
            <div className="flex min-w-0 flex-col">
              <h1 className="font-serif text-[clamp(2.75rem,6.6vw,4.5rem)] leading-[0.94] font-normal tracking-[-0.01em] text-balance [&_em]:text-brand [&_em]:italic">
                The meter starts and stops with your <em>code</em>.
              </h1>
              <p className="mt-6 max-w-[30rem] text-[15px] leading-[1.6] text-muted-foreground sm:text-base">
                Compute billing starts with the container and stops with it. You pay by the second.
              </p>
              <div className="mt-8 flex flex-col gap-2.5 sm:flex-row sm:flex-wrap">
                <GetStartedButton className="marketing-action-primary stamp border-brand/45" />
                <MarketingButton
                  className="marketing-action-secondary stamp-quiet border-input"
                  endGlyph="↓"
                  hash="plans"
                  to="/pricing"
                >
                  See the plans
                </MarketingButton>
              </div>
            </div>

            <MarketingCard className="min-w-0 p-5 sm:p-6">
              <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-4">
                <h2 className="font-serif text-[clamp(1.625rem,3vw,2.125rem)] leading-none font-normal">
                  Resource costs
                </h2>
                <MeterToggle controls={fleetRatesId} meter={meter} onChange={setMeter} />
              </div>
              <RateList
                groups={[...computeGroups(placement, meter), ...platformGroups(catalog)]}
                id={fleetRatesId}
              />
              <a
                className="interactive-link text-[12.5px] text-muted-foreground underline underline-offset-4"
                href={new URL("/platform/plans#compute-pricing", DOCS_URL).href}
              >
                Pricing details
              </a>
            </MarketingCard>
          </div>
        </section>

        <section className="border-b border-border bg-muted py-14 sm:py-16 lg:py-20" id="plans">
          <div className={shell}>
            <div className="mb-6 max-w-[44rem]">
              <h2 className={sectionTitle}>Pricing plans</h2>
            </div>
            <div className="grid grid-cols-3 gap-4 max-md:grid-cols-1">
              {catalog.plans.map((plan) => (
                <MarketingCard asChild key={plan.terms_version}>
                  <article className="flex flex-col p-5 sm:p-6">
                    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                      <h3 className="font-serif text-[24px] leading-none font-normal">
                        {plan.name}
                      </h3>
                      <p className="flex items-baseline gap-2">
                        <span className="font-mono text-[22px] leading-none tracking-[-0.02em]">
                          {exactDollars(plan.monthly_nanos)}
                        </span>
                        <span className="text-[12px] text-muted-foreground">per month</span>
                      </p>
                    </div>
                    <p className="mt-3 text-[13px] leading-relaxed text-muted-foreground">
                      {plan.summary}
                    </p>
                    <dl className="mt-4 border-t border-border text-[13px]">
                      <div className="flex items-baseline justify-between gap-4 border-b border-border py-2.5">
                        <dt className="text-muted-foreground">Monthly usage credit</dt>
                        <dd className="font-mono font-medium text-brand">
                          {exactDollars(plan.included_nanos)}{" "}
                          <span className="text-muted-foreground">/ month</span>
                        </dd>
                      </div>
                      <PlanLimit
                        label="CPU containers at once"
                        value={plan.entitlements.max_concurrent_cpu_containers.toLocaleString()}
                      />
                      <PlanLimit
                        label="GPU cards at once"
                        value={plan.entitlements.max_concurrent_gpus.toLocaleString()}
                      />
                      <PlanLimit
                        label="GPU models"
                        value={gpuModelsLabel(plan.entitlements.gpu_types)}
                      />
                      <PlanLimit
                        label="Workspaces"
                        value={limitFigure(plan.entitlements.max_workspaces)}
                      />
                      <PlanLimit
                        label="Members"
                        value={memberLimitFigure(plan.entitlements.max_members)}
                      />
                      <PlanFeature
                        label="Connected cloud"
                        included={plan.entitlements.connected_cloud}
                      />
                      <PlanFeature
                        label="Custom domains"
                        included={plan.entitlements.custom_domains}
                      />
                      <PlanFeature label="Self-hosted" included={plan.entitlements.self_hosted} />
                      <PlanLimit
                        label="Log retention"
                        value={`${plan.entitlements.log_retention_days} days`}
                      />
                    </dl>
                    <ul className="mt-4 mb-6 grid list-none gap-2 p-0">
                      {plan.terms.map((term) => (
                        <PlanTerm key={term}>{term}</PlanTerm>
                      ))}
                    </ul>
                    <GetStartedButton className="marketing-action-secondary stamp-quiet mt-auto w-full border-input" />
                  </article>
                </MarketingCard>
              ))}
            </div>

            <p className="mt-4 flex max-w-[58rem] items-start gap-2.5 text-[12.5px] leading-relaxed text-muted-foreground">
              <span className="mt-0.5 shrink-0 text-brand">
                <Glyph>↳</Glyph>
              </span>
              <span>{accountTerm(catalog)}</span>
            </p>
          </div>
        </section>

        <FinalCta
          title={
            <>
              Pay for the resources <em>that ran.</em>
            </>
          }
          body="Applications, jobs, GPU workloads, and sandboxes use the same meter and draw from one balance."
        />
      </main>
    </MarketingLayout>
  );
}

/* One term of a plan, read off the catalog. The value column is monospaced
   whether it holds a figure, a list of models, or a word, so the eye runs down
   one column rather than two typefaces. */
function PlanLimit({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border py-2.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-mono font-medium">{value}</dd>
    </div>
  );
}

function PlanFeature({ label, included }: { label: string; included: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border py-2.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-mono font-medium">{included ? "Included" : "—"}</dd>
    </div>
  );
}

function PlanTerm({ children }: { children: React.ReactNode }) {
  return (
    <li className="flex items-start gap-2.5 text-[13px] leading-relaxed">
      <span className="mt-0.5 shrink-0 text-brand">
        <Glyph>↳</Glyph>
      </span>
      <span>{children}</span>
    </li>
  );
}

/* A group's heading sits in its own column beside the first price rather than
   above the block, so the eye runs down one column of resources and one of
   money. */
function RateList({ groups, id }: { groups: readonly RateGroup[]; id: string }) {
  return (
    <div className="mt-6" id={id}>
      {groups.map((group) => (
        <div className="border-t border-border py-5" key={group.heading ?? group.lines[0].label}>
          {group.heading ? <h3 className="mb-4 text-[13px] font-medium">{group.heading}</h3> : null}
          <dl className="space-y-3">
            {group.lines.map((line) => (
              <div className="flex items-baseline justify-between gap-4" key={line.label}>
                <dt className="min-w-0 text-[13px] leading-snug text-muted-foreground">
                  {line.label}
                </dt>
                <dd className="shrink-0 font-mono text-[13px] whitespace-nowrap">
                  {typeof line.figure === "number" ? <Rate nanos={line.figure} /> : line.figure}{" "}
                  <span className="text-muted-foreground">{line.unit}</span>
                </dd>
              </div>
            ))}
          </dl>
        </div>
      ))}
    </div>
  );
}

/* Native radios rather than a scripted segment: arrow keys move and choose, the
   legend names the group, and each option announces that it is the selected one
   without any of that being reimplemented. */
function MeterToggle({
  meter,
  onChange,
  controls,
}: {
  meter: Meter;
  onChange: (next: Meter) => void;
  controls: string;
}) {
  return (
    <fieldset className="shrink-0">
      <legend className="sr-only">Read every rate per hour or per second</legend>
      <div className="flex rounded-full border border-border bg-card p-1">
        {meters.map((option) => (
          <label className="cursor-pointer" key={option.value}>
            <input
              aria-controls={controls}
              checked={option.value === meter}
              className="peer sr-only"
              name="rate-meter"
              onChange={() => onChange(option.value)}
              type="radio"
              value={option.value}
            />
            <span className="inline-flex min-h-8 items-center rounded-full px-3.5 text-[12px] font-medium text-muted-foreground transition-colors hover:text-foreground peer-checked:bg-brand peer-checked:font-semibold peer-checked:text-brand-foreground peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-brand [@media(pointer:coarse)]:min-h-11">
              {option.label}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/* A per-second rate is mostly leading zeros, and the length of that run is the
   part of it the eye compares two rows by. Dimming the run leaves the exact
   published figure on the page while the significant digits carry the scan. */
function Rate({ nanos }: { nanos: number }) {
  const figure = exactDollars(nanos);
  const significant = figure.search(/[1-9]/);
  if (significant <= 0) {
    return figure;
  }
  return (
    <>
      <span className="text-muted-foreground">{figure.slice(0, significant)}</span>
      {figure.slice(significant)}
    </>
  );
}

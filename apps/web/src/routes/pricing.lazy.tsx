import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { createLazyFileRoute } from "@tanstack/react-router";
import { ArrowDown, CornerDownRight } from "lucide-react";

import type { PricingCatalog, PublishedPlacementRate } from "@/lib/api/schemas";
import { gpuModelsLabel, limitFigure, memberLimitFigure } from "@/lib/entitlements";
import { DOCS_URL } from "@/lib/env";
import { countLabel } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import { pricingCatalogQueryOptions } from "@/lib/queries/pricing";

import { MarketingLayout } from "./-marketing/MarketingLayout";
import { MarketingReveal } from "./-marketing/MarketingReveal";
import {
  FinalCta,
  GetStartedButton,
  MarketingButton,
  MarketingCard,
  MarketingHero,
  shell,
} from "./-marketing/MarketingPrimitives";
import "./pricing.css";

export const Route = createLazyFileRoute("/pricing")({
  component: MarketingPricing,
});

const SECONDS_PER_HOUR = 3600;

type Meter = "hour" | "second";

function metered(nanosPerHour: number, meter: Meter): number {
  return meter === "hour" ? nanosPerHour : nanosPerHour / SECONDS_PER_HOUR;
}

const meters = [
  { value: "hour", label: "Per hour" },
  { value: "second", label: "Per second" },
] as const satisfies readonly { value: Meter; label: string }[];

type RateLine = {
  label: string;
  figure: number | string;
  unit: string;
  fractionDigits?: number;
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
        fractionDigits: meter === "hour" ? 2 : 6,
      })),
    },
    {
      heading: "CPU and memory",
      lines: [
        {
          label: "CPU",
          figure: metered(shape.nanos_per_cpu_core_hour, meter),
          unit: `/ CPU / ${per}`,
          fractionDigits: meter === "hour" ? 4 : 8,
        },
        {
          label: "Memory",
          figure: metered(shape.nanos_per_memory_gib_hour, meter),
          unit: `/ GiB / ${per}`,
          fractionDigits: meter === "hour" ? 4 : 8,
        },
      ],
    },
  ];
}

function platformGroups(catalog: PricingCatalog): readonly RateGroup[] {
  return [
    {
      heading: "Storage and infrastructure",
      lines: [
        {
          label: "Volumes",
          figure: catalog.platform_rate.nanos_per_volume_gib_month,
          unit: "/ GiB / 30 days",
        },
        {
          label: "Egress",
          figure: catalog.platform_rate.nanos_per_egress_gib,
          unit: "/ GiB",
        },
        {
          label: "Bring your own cloud",
          figure: `${catalog.connected_cloud_management_fee_percent}%`,
          unit: "management fee",
        },
      ],
    },
  ];
}

function accountTerm(catalog: PricingCatalog): string {
  const terms = catalog.no_payment_method;
  return `New accounts get ${formatCostNanos(catalog.trial.amount_nanos)} in one-time trial credit, valid for ${catalog.trial.duration_days} days. Without a saved card, the limit is ${countLabel(terms.max_concurrent_cpu_containers, "CPU container")} and ${countLabel(terms.max_concurrent_gpus, "GPU card")} at once, using ${gpuModelsLabel(terms.gpu_types)}. Add a card and credit to use all offered GPU models on any plan, subject to availability. Monthly plan credit expires at the end of the billing period. Purchased credit never expires.`;
}

const sectionTitle =
  "font-sans text-[clamp(1.75rem,4.2vw,2.5rem)] leading-[1.08] font-[550] tracking-[-0.045em] text-balance [&_em]:text-brand [&_em]:not-italic";

function MarketingPricing() {
  const [meter, setMeter] = useState<Meter>("hour");
  const fleetRatesId = useId();
  const pricing = useQuery(pricingCatalogQueryOptions());
  const catalog = pricing.data;

  const placement = catalog?.placement_rates.find((rate) => rate.rate_class === "auto");
  if (catalog && !placement) throw new Error("the pricing catalog has no base compute rates");

  return (
    <MarketingLayout>
      <main className="marketing-hero-page" id="marketing-main">
        <MarketingHero className="pricing-hero">
          <div className="flex min-w-0 flex-col">
            <h1 className="max-w-[620px] text-balance">
              Compute pricing <em>by the second.</em>
            </h1>
            <p className="mt-5 max-w-[540px] text-base leading-[1.58] text-muted-foreground sm:mt-6 sm:text-lg">
              Pay for compute while your container is running.
            </p>
            <div className="mt-7 flex flex-col gap-2.5 sm:flex-row sm:flex-wrap">
              <GetStartedButton />
              <MarketingButton
                endIcon={<ArrowDown aria-hidden="true" />}
                hash="plans"
                to="/pricing"
              >
                Compare plans
              </MarketingButton>
            </div>
          </div>

          <MarketingCard surface="frame" className="flex min-w-0 flex-col p-4 sm:p-6">
            <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-4">
              <h2 className="font-sans text-[clamp(1.625rem,3vw,2.125rem)] leading-[1.08] font-[550] tracking-[-0.045em]">
                Usage rates
              </h2>
              <MeterToggle
                controls={fleetRatesId}
                disabled={!catalog}
                meter={meter}
                onChange={setMeter}
              />
            </div>
            <div className="mt-4" id={fleetRatesId} aria-busy={pricing.isPending}>
              {catalog && placement ? (
                <RateList
                  groups={[...computeGroups(placement, meter), ...platformGroups(catalog)]}
                />
              ) : (
                <div className="border-t border-border py-5">
                  <p
                    className={
                      pricing.error ? "text-sm text-destructive" : "text-sm text-muted-foreground"
                    }
                    role={pricing.error ? "alert" : "status"}
                  >
                    {pricing.error?.message ?? "Loading current pricing…"}
                  </p>
                </div>
              )}
            </div>
            <a
              className="interactive-link mt-5 text-[12.5px] text-muted-foreground underline underline-offset-4"
              href={new URL("/platform/plans#compute-pricing", DOCS_URL).href}
            >
              Pricing details
            </a>
          </MarketingCard>
        </MarketingHero>

        <section
          className="pricing-plans border-b border-border bg-background-subtle py-6"
          id="plans"
        >
          <div className={shell}>
            <MarketingReveal className="mb-5 max-w-[44rem]">
              <h2 className={sectionTitle}>Pricing plans</h2>
            </MarketingReveal>
            {catalog ? (
              <>
                <MarketingReveal className="grid grid-cols-1 gap-4 lg:grid-cols-3" delay={100}>
                  {catalog.plans.map((plan) => (
                    <MarketingCard surface="frame" asChild key={plan.terms_version}>
                      <article className="flex min-w-0 flex-col p-4 sm:p-5">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <h3 className="font-sans text-[24px] leading-[1.08] font-[550] tracking-[-0.045em]">
                            {plan.name}
                          </h3>
                          <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                            <span className="font-sans text-[36px] leading-none font-medium tracking-[-0.055em] tabular-nums">
                              {formatCostNanos(plan.monthly_nanos)}
                            </span>
                            <span className="text-[12px] text-muted-foreground">per month</span>
                          </p>
                        </div>
                        <MarketingCard surface="raised" className="my-4 px-3 py-2.5" asChild>
                          <dl className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-[13px]">
                            <dt className="text-muted-foreground">Monthly usage credit</dt>
                            <dd className="font-mono font-medium text-brand">
                              {formatCostNanos(plan.included_nanos)}
                            </dd>
                          </dl>
                        </MarketingCard>
                        <MarketingCard
                          surface="inset"
                          className="px-3 py-1 text-[13px] [--surface-grain-blend:soft-light]"
                          asChild
                        >
                          <dl>
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
                            <PlanLimit
                              label="Connected cloud"
                              value={
                                plan.entitlements.connected_cloud ? "Included" : "Not included"
                              }
                            />
                            <PlanLimit
                              label="Custom domains"
                              value={plan.entitlements.custom_domains ? "Included" : "Not included"}
                            />
                            <PlanLimit
                              label="Self-hosted"
                              value={plan.entitlements.self_hosted ? "Included" : "Not included"}
                            />
                            <PlanLimit
                              label="Log and artifact retention"
                              value={countLabel(plan.entitlements.retention_days, "day")}
                            />
                          </dl>
                        </MarketingCard>
                        <details className="my-3 text-[12px] text-muted-foreground">
                          <summary className="cursor-pointer py-1.5 underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand">
                            {plan.name} plan terms
                          </summary>
                          <ul className="mt-2 mb-3 grid list-disc gap-2 pl-4 leading-relaxed">
                            {plan.terms.map((term) => (
                              <li key={term}>{term}</li>
                            ))}
                          </ul>
                        </details>
                        <GetStartedButton variant="secondary" className="mt-auto w-full" />
                      </article>
                    </MarketingCard>
                  ))}
                </MarketingReveal>

                <p className="mt-4 flex items-start gap-2.5 text-xs leading-relaxed text-muted-foreground">
                  <span className="mt-0.5 shrink-0 text-brand">
                    <CornerDownRight className="size-4 shrink-0" aria-hidden="true" />
                  </span>
                  <span>{accountTerm(catalog)}</span>
                </p>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                {pricing.error ? "Plan pricing is unavailable." : "Loading plans…"}
              </p>
            )}
          </div>
        </section>

        <FinalCta
          title={
            <>
              Choose a plan <em>and start deploying.</em>
            </>
          }
          body="Workload usage draws from your account balance. Paid plans include monthly usage credit."
        />
      </main>
    </MarketingLayout>
  );
}

function PlanLimit({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/50 py-1.5 last:border-b-0">
      <dt className="min-w-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-right font-mono text-xs font-medium">{value}</dd>
    </div>
  );
}

function RateList({ groups }: { groups: readonly RateGroup[] }) {
  return (
    <div className="space-y-3">
      {groups.map((group) => (
        <MarketingCard
          surface="inset"
          className="px-3 py-3 [--surface-grain-blend:soft-light]"
          key={group.heading ?? group.lines[0].label}
        >
          {group.heading ? (
            <h3 className="mb-2.5 text-[12px] font-medium">{group.heading}</h3>
          ) : null}
          <dl className="space-y-1.5">
            {group.lines.map((line) => (
              <div
                className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1"
                key={line.label}
              >
                <dt className="min-w-0 text-[13px] leading-snug text-muted-foreground">
                  {line.label}
                </dt>
                <dd className="shrink-0 font-mono text-[13px] whitespace-nowrap">
                  {typeof line.figure === "number"
                    ? formatCostNanos(line.figure, "USD", line.fractionDigits ?? 4)
                    : line.figure}{" "}
                  <span className="text-muted-foreground">{line.unit}</span>
                </dd>
              </div>
            ))}
          </dl>
        </MarketingCard>
      ))}
    </div>
  );
}

function MeterToggle({
  meter,
  onChange,
  controls,
  disabled,
}: {
  meter: Meter;
  onChange: (next: Meter) => void;
  controls: string;
  disabled: boolean;
}) {
  return (
    <fieldset className="shrink-0" disabled={disabled}>
      <legend className="sr-only">Read every rate per hour or per second</legend>
      <MarketingCard surface="inset" className="flex p-1">
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
            <span className="inline-flex min-h-8 items-center rounded-md px-3.5 text-[12px] font-medium text-muted-foreground transition-colors hover:text-foreground peer-checked:bg-brand peer-checked:font-semibold peer-checked:text-brand-foreground peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-brand [@media(pointer:coarse)]:min-h-11">
              {option.label}
            </span>
          </label>
        ))}
      </MarketingCard>
    </fieldset>
  );
}

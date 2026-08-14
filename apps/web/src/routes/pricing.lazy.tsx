import { useState } from "react";
import { createLazyFileRoute } from "@tanstack/react-router";

import { exactDollars } from "@/lib/money";
import { cn } from "@/lib/utils";

import { MarketingLayout } from "./-marketing/MarketingLayout";
import {
  Faq,
  FinalCta,
  Glyph,
  MarketingButton,
  PendingMarketingButton,
  Pill,
  SectionHeading,
  shell,
} from "./-marketing/MarketingPrimitives";
import {
  BILLING_CURRENCY,
  EGRESS_NANOS_PER_GIB,
  RATES_EFFECTIVE_ON,
  VOLUME_STORAGE_NANOS_PER_GIB_MONTH,
  planIds,
  publishedGpuRates,
  publishedPlans,
  publishedShapeRates,
  type PlanId,
} from "./-marketing/pricingCatalog";

export const Route = createLazyFileRoute("/pricing")({
  component: MarketingPricing,
});

const fleet = publishedShapeRates.platform_fleet;
const connected = publishedShapeRates.connected_cloud;

/* What each plan is called and what it promises. The two figures beside them are
   the rate card's, so a plan the card publishes with nothing written for it here
   does not compile. */
const planCopy: Record<PlanId, { name: string; summary: string; terms: readonly string[] }> = {
  free: {
    name: "Free",
    summary: "What an account costs before it has agreed to anything.",
    terms: [
      "Every workload the platform runs: applications, APIs, functions, jobs, queues, schedules, and sandboxes.",
      "No payment method needed to start. Usage past the included compute is billed at the rates below on the same invoice.",
      "Work is refused only when a payment cannot be collected. Nothing is stopped mid-flight over a bill.",
    ],
  },
  team: {
    name: "Team",
    summary: "A monthly subscription that comes with compute included.",
    terms: [
      "The same workloads and the same rates. A plan changes what you pay, not what you can run.",
      "Usage past the included compute is billed at the rates below on the same invoice, automatically.",
      "The included compute is issued each period as credit and is spent against usage, not banked.",
    ],
  },
};

const plans = planIds.map((id) => ({ id, ...publishedPlans[id], ...planCopy[id] }));

/* Everything a container is charged for, both columns, in one list. The GPU rows
   come from the catalog; these are the parts every container pays. */
const shapeRows = [
  {
    id: "container",
    resource: "Container",
    unitNoun: "container",
    fleet: fleet.nanosPerContainerHour,
    connected: connected.nanosPerContainerHour,
  },
  {
    id: "cpu",
    resource: "Processor",
    unitNoun: "CPU",
    fleet: fleet.nanosPerCpuCoreHour,
    connected: connected.nanosPerCpuCoreHour,
  },
  {
    id: "memory",
    resource: "Memory",
    unitNoun: "GiB",
    fleet: fleet.nanosPerMemoryGibHour,
    connected: connected.nanosPerMemoryGibHour,
  },
] as const;

const platformRows = [
  {
    id: "egress",
    resource: "Network egress",
    unitNoun: "GiB",
    nanos: EGRESS_NANOS_PER_GIB,
    note: "Traffic leaving the platform. Metered on every container and included.",
  },
  {
    id: "volume-storage",
    resource: "Volume storage",
    unitNoun: "GiB-month",
    nanos: VOLUME_STORAGE_NANOS_PER_GIB_MONTH,
    note: "Persistent volumes, measured continuously while they exist. Included.",
  },
] as const;

const placements = [
  {
    title: "LazyCloud compute",
    body: "Capacity we buy and run. The rate is the whole cost of the machine and the platform on it.",
  },
  {
    title: "Your connected cloud",
    body: "Capacity we provision into your own AWS account. Your provider bills you for the machine; this is the management fee on what we placed there.",
  },
  {
    title: "Machines you join",
    body: "Hardware you already own, joined to a workspace as capacity. We neither buy it nor manage it, so every rate on it is published at zero.",
  },
];

const meterRules = [
  {
    title: "Held or used, whichever is greater",
    body: "Each resource is charged for the greater of what a container asked for and what it actually used. Capacity you hold is capacity nobody else can be given, so it is paid for whether or not it is busy — and a container that bursts past its request pays for the burst at the same rate. A GPU is held whole and charged whole.",
  },
  {
    title: "One balance, in dollars",
    body: "Included compute is money, not hours. The same balance covers processors, memory, and any GPU, so nothing is stranded in a bucket you have no way to spend.",
  },
  {
    title: "Priced when it ran",
    body: "A container's rate is fixed at the instant it ran and frozen onto the record of the charge. A price change moves what tomorrow costs, never what yesterday did, and a run spanning one is split at the moment it changed.",
  },
  {
    title: "Zero is a price",
    body: "Egress, volume storage and joined machines are all metered and all published at zero. They appear on your usage and on your invoice reading $0.00, which is how you can tell they are measured rather than ignored.",
  },
];

const faqItems = [
  {
    question: "What does a container actually cost?",
    answer:
      "At least the rates for what you asked for: the processors, the memory, and any GPU, for as long as you hold them. A one-processor, two-gibibyte container with no GPU on LazyCloud compute costs the processor rate plus twice the memory rate for every hour it runs. If it uses more than one processor — the request is a floor, not a cap — the processor rate applies to what it actually used instead.",
  },
  {
    question: "What happens when the included compute is spent?",
    answer:
      "On both plans, usage past the included amount is billed at the rates below on the same invoice, automatically. Work already running keeps running and keeps being metered, and new work is refused only when a payment cannot be collected — so keeping a working card on file is what keeps an account running.",
  },
  {
    question: "Is egress or storage going to start costing money?",
    answer:
      "Not without this page changing first. Both are metered today and priced at zero, and the figure on this page is read from the same rate card the platform bills from — so the day either becomes non-zero is the day it is published here.",
  },
  {
    question: "Is there an enterprise plan?",
    answer:
      "No. There are two plans and one rate card. What a plan changes is the subscription and how much compute comes with it, not which workloads, regions, or hardware you can reach.",
  },
  {
    question: "Which currency are rates in?",
    answer:
      "US dollars. Rates are stored in nanodollars — billionths of a dollar — so a second of even the cheapest resource carries an exact price rather than a rounded one, and the hourly figures here are those exact rates converted without rounding.",
  },
];

function MarketingPricing() {
  return (
    <MarketingLayout>
      <main id="marketing-main">
        <section className="relative overflow-hidden border-b border-border bg-background">
          <div className="marketing-grid-field" aria-hidden="true" />
          <div
            className={cn(
              shell,
              "relative z-[2] grid grid-cols-[0.92fr_1.08fr] items-center gap-10 pt-12 pb-16 sm:gap-12 sm:pt-16 sm:pb-20 lg:gap-16 lg:pt-22 lg:pb-24 max-lg:grid-cols-1",
            )}
          >
            <div>
              <Pill>Metered by the resource · billed in dollars</Pill>
              <h1 className="mt-4 max-w-[560px] font-serif text-[clamp(40px,7.5vw,76px)] leading-[0.98] font-normal tracking-[-0.005em] text-balance sm:mt-5 [&_em]:text-brand [&_em]:italic">
                Two plans. <em>One meter.</em>
              </h1>
              <p className="mt-5 max-w-[520px] text-base leading-[1.58] text-muted-foreground sm:mt-6 sm:text-lg">
                Each plan comes with an amount of compute in dollars rather than hours. Spend it on
                anything the platform runs—processors, memory, or any GPU—and pay the published rate
                for whatever you use beyond it.
              </p>
              <div className="mt-7 flex flex-col gap-2.5 sm:flex-row sm:flex-wrap">
                <PendingMarketingButton className="marketing-action-primary stamp border-brand/45">
                  Private beta
                </PendingMarketingButton>
                <MarketingButton
                  className="marketing-action-secondary stamp-quiet border-input"
                  endGlyph="↓"
                  hash="rates"
                  to="/pricing"
                >
                  Read the rate card
                </MarketingButton>
              </div>
            </div>

            <ContainerQuote />
          </div>
        </section>

        <section className="border-b border-border bg-muted py-16 sm:py-20 lg:py-24">
          <div className={shell}>
            <SectionHeading
              label="Plans"
              title={
                <>
                  Pay nothing, or pay <em>for the month.</em>
                </>
              }
              body="Both plans meter the same way at the same rates. What a plan buys is a larger balance to spend before anything is billed."
            />
            <div className="grid grid-cols-2 gap-5 max-md:grid-cols-1">
              {plans.map((plan) => (
                <article
                  className="flex flex-col rounded-2xl border border-border bg-card p-6 sm:p-8"
                  key={plan.id}
                >
                  <h3 className="font-serif text-[30px] leading-none font-normal">{plan.name}</h3>
                  <p className="mt-5 flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
                    <span className="font-mono text-[clamp(2.25rem,6vw,3rem)] leading-none tracking-[-0.03em]">
                      {exactDollars(plan.monthlyNanos)}
                    </span>
                    <span className="text-[13px] text-muted-foreground">per month</span>
                  </p>
                  <p className="mt-5 rounded-xl border border-brand/25 bg-brand/8 px-4 py-3.5 text-[13px] leading-relaxed">
                    <strong className="font-mono font-semibold text-brand">
                      {exactDollars(plan.includedNanos)}
                    </strong>{" "}
                    of compute included every month, across every metric.
                  </p>
                  <p className="mt-5 text-[13px] text-muted-foreground">{plan.summary}</p>
                  <ul className="mt-4 mb-7 grid list-none gap-2.5 p-0">
                    {plan.terms.map((term) => (
                      <li
                        className="flex items-start gap-2.5 text-[13px] leading-relaxed"
                        key={term}
                      >
                        <span className="mt-0.5 shrink-0 text-brand">
                          <Glyph>↳</Glyph>
                        </span>
                        <span>{term}</span>
                      </li>
                    ))}
                  </ul>
                  <PendingMarketingButton className="marketing-action-secondary stamp-quiet mt-auto w-full border-input">
                    Private beta
                  </PendingMarketingButton>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section
          className="border-b border-border bg-background py-16 sm:py-20 lg:py-24"
          id="rates"
        >
          <div className={shell}>
            <SectionHeading
              label="Rates"
              title={
                <>
                  The whole rate card, <em>in one page.</em>
                </>
              }
              body="Each rate below buys one resource for one hour. A container is charged for every resource it holds, for as long as it holds it, and for whatever it uses above that — with no minimum charge and nothing rounded up to a whole hour."
            />
            <div className="mb-5">
              <Pill>
                Effective {RATES_EFFECTIVE_ON} · {BILLING_CURRENCY} · dollars per hour
              </Pill>
            </div>
            {/* The notes sit beside the columns they name, which also keeps the
                figures within a short scan of the resource they price. */}
            <div className="grid grid-cols-[1.6fr_1fr] items-start gap-5 max-lg:grid-cols-1">
              <div className="overflow-x-auto rounded-2xl border border-border bg-card">
                <table className="w-full border-collapse text-left">
                  <caption className="sr-only">
                    Compute rates in US dollars per hour, on LazyCloud capacity and on a connected
                    cloud account
                  </caption>
                  <thead>
                    <tr className="border-b border-border bg-muted/50">
                      <th
                        className="px-2.5 py-3 text-[11px] font-medium text-muted-foreground sm:px-5"
                        scope="col"
                      >
                        Resource
                      </th>
                      <th
                        className="px-2.5 py-3 text-right text-[11px] font-medium text-muted-foreground sm:px-5"
                        scope="col"
                      >
                        LazyCloud compute
                      </th>
                      <th
                        className="px-2.5 py-3 text-right text-[11px] font-medium text-muted-foreground sm:px-5"
                        scope="col"
                      >
                        Your connected cloud
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {shapeRows.map((row) => (
                      <RateRow
                        key={row.id}
                        resource={row.resource}
                        unitNoun={row.unitNoun}
                        fleet={row.fleet}
                        connected={row.connected}
                      />
                    ))}
                    {publishedGpuRates.map((rate) => (
                      <RateRow
                        key={rate.gpuType}
                        resource={rate.gpuType}
                        unitNoun="GPU"
                        fleet={rate.platformFleetNanosPerCardHour}
                        connected={rate.connectedCloudNanosPerCardHour}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="grid gap-4 max-lg:grid-cols-3 max-md:grid-cols-1">
                {placements.map((placement) => (
                  <article
                    className="rounded-xl border border-border bg-card p-5"
                    key={placement.title}
                  >
                    <h3 className="text-[15px] font-medium tracking-[-0.01em]">
                      {placement.title}
                    </h3>
                    <p className="mt-2 text-[13px] leading-relaxed text-muted-foreground">
                      {placement.body}
                    </p>
                  </article>
                ))}
              </div>
            </div>

            <div className="mt-5 grid grid-cols-2 gap-4 max-md:grid-cols-1">
              {platformRows.map((row) => (
                <article
                  className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2 rounded-xl border border-border bg-card p-5"
                  key={row.id}
                >
                  <div className="min-w-0">
                    <h3 className="text-[15px] font-medium tracking-[-0.01em]">{row.resource}</h3>
                    <p className="mt-2 text-[13px] leading-relaxed text-muted-foreground">
                      {row.note}
                    </p>
                  </div>
                  <p className="font-mono text-[22px] leading-none tracking-[-0.02em] text-brand">
                    {exactDollars(row.nanos)}
                    <span className="ml-1.5 font-sans text-[11px] text-muted-foreground">
                      per {row.unitNoun}
                    </span>
                  </p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="border-b border-border bg-muted py-16 sm:py-20 lg:py-24">
          <div className={shell}>
            <SectionHeading
              label="How the meter reads"
              title={
                <>
                  What you are charged, <em>and when.</em>
                </>
              }
            />
            <div className="grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-border bg-border max-md:grid-cols-1">
              {meterRules.map((rule) => (
                <article className="bg-card p-6 sm:p-7" key={rule.title}>
                  <h3 className="font-mono text-[12px] tracking-[0.04em] text-brand uppercase">
                    {rule.title}
                  </h3>
                  <p className="mt-3 text-[13px] leading-relaxed text-muted-foreground sm:text-sm">
                    {rule.body}
                  </p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <Faq items={faqItems} />

        <FinalCta
          title={
            <>
              Pay for the resources <em>that actually ran.</em>
            </>
          }
          body="Applications, jobs, GPU workloads, and sandboxes all meter the same way, on one balance you can spend anywhere."
        />
      </main>
    </MarketingLayout>
  );
}

function RateRow({
  resource,
  unitNoun,
  fleet,
  connected,
}: {
  resource: string;
  unitNoun: string;
  fleet: number;
  connected: number;
}) {
  return (
    <tr className="border-b border-border last:border-b-0">
      <th className="px-2.5 py-3 font-medium sm:px-5" scope="row">
        <span className="text-[13px]">{resource}</span>
        <span className="block font-mono text-[10px] font-normal text-muted-foreground">
          per {unitNoun}-hour
        </span>
      </th>
      <td className="px-2.5 py-3 text-right font-mono text-[11.5px] sm:px-5 sm:text-[13px]">
        {exactDollars(fleet)}
      </td>
      <td className="px-2.5 py-3 text-right font-mono text-[11.5px] text-muted-foreground sm:px-5 sm:text-[13px]">
        {exactDollars(connected)}
      </td>
    </tr>
  );
}

/* A rate card is a list of parts, and the question people actually have is what
   one container costs. Choosing a shape adds its parts up on the page, which is
   the floor the pricer charges for holding that shape — stated as a floor,
   because the same widget saying "this is what it costs" would be quoting a
   figure a busy container goes past. */
function ContainerQuote() {
  const [gpuType, setGpuType] = useState<string>("");
  const [cores, setCores] = useState(1);
  const gpu = publishedGpuRates.find((rate) => rate.gpuType === gpuType);
  const memoryGib = gpu ? cores * 4 : cores * 2;
  const nanosPerHour =
    fleet.nanosPerContainerHour +
    fleet.nanosPerCpuCoreHour * cores +
    fleet.nanosPerMemoryGibHour * memoryGib +
    (gpu?.platformFleetNanosPerCardHour ?? 0);

  return (
    <div className="min-w-0 rounded-2xl border border-input bg-card shadow-[8px_8px_0_0_var(--secondary)]">
      <div className="flex min-h-13 flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-border px-4 py-3 sm:px-5">
        <strong className="text-[13px] font-semibold">What a container costs to hold</strong>
        <span className="font-mono text-[10px] tracking-[0.08em] text-muted-foreground uppercase">
          {BILLING_CURRENCY} · LazyCloud compute
        </span>
      </div>

      <fieldset className="border-b border-border px-4 py-3.5 sm:px-5">
        <legend className="sr-only">Choose a GPU, or none</legend>
        <div className="flex flex-wrap gap-1.5">
          {[
            { gpuType: "", label: "CPU only" },
            ...publishedGpuRates.map((rate) => ({ gpuType: rate.gpuType, label: rate.gpuType })),
          ].map((option) => (
            <label className="cursor-pointer" key={option.gpuType || "cpu"}>
              <input
                checked={option.gpuType === gpuType}
                className="peer sr-only"
                name="quote-gpu"
                onChange={() => setGpuType(option.gpuType)}
                type="radio"
                value={option.gpuType}
              />
              <span className="inline-flex min-h-8 items-center rounded-md border border-border px-2.5 font-mono text-[11px] text-muted-foreground transition-colors hover:border-brand/40 hover:text-foreground peer-checked:border-brand peer-checked:bg-brand/10 peer-checked:font-medium peer-checked:text-brand peer-focus-visible:outline-2 peer-focus-visible:outline-offset-3 peer-focus-visible:outline-brand [@media(pointer:coarse)]:min-h-11">
                {option.label}
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3.5 sm:px-5">
        <legend className="sr-only">Choose how many processors</legend>
        <label className="text-[12px] text-muted-foreground" htmlFor="quote-cores">
          Processors
        </label>
        <input
          className="h-8 w-20 rounded-md border border-border bg-background px-2 font-mono text-[12px]"
          id="quote-cores"
          max={64}
          min={1}
          onChange={(event) => setCores(Math.max(1, Math.min(64, Number(event.target.value) || 1)))}
          type="number"
          value={cores}
        />
        <span className="font-mono text-[12px] text-muted-foreground">
          + {memoryGib} GiB memory
        </span>
      </fieldset>

      <div className="px-4 py-5 sm:px-5">
        <p className="font-mono text-[clamp(1.75rem,4.4vw,2.375rem)] leading-none tracking-[-0.03em]">
          {exactDollars(nanosPerHour)}
        </p>
        <p className="mt-2.5 text-[12px] text-muted-foreground">
          per hour it holds this shape. Use more than {cores}{" "}
          {cores === 1 ? "processor" : "processors"} or {memoryGib} GiB and the excess is charged at
          the same rates.
        </p>
      </div>

      <p className="flex items-start gap-2 border-t border-border px-4 py-3.5 text-[11px] leading-relaxed text-muted-foreground sm:px-5">
        <span className="mt-0.5 shrink-0 text-brand">
          <Glyph>↳</Glyph>
        </span>
        <span>
          {planCopy.free.name} includes {exactDollars(publishedPlans.free.includedNanos)} a month,
          which is {allowanceHours(publishedPlans.free.includedNanos, nanosPerHour)} of this
          container inside its request. Egress and volume storage are metered alongside it and cost
          nothing.
        </span>
      </p>
    </div>
  );
}

/** How far an allowance goes, floored so the figure is never flattering. */
function allowanceHours(includedNanos: number, nanosPerHour: number): string {
  if (nanosPerHour <= 0) return "unlimited hours";
  const hours = includedNanos / nanosPerHour;
  if (hours >= 1) return `${Math.floor(hours * 10) / 10} hours`;
  return `${Math.floor(hours * 60)} minutes`;
}

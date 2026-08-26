import { useId, useState } from "react";
import { createLazyFileRoute } from "@tanstack/react-router";

import { exactDollars } from "@/lib/money";
import { cn } from "@/lib/utils";

import { MarketingLayout } from "./-marketing/MarketingLayout";
import {
  FinalCta,
  Glyph,
  GetStartedButton,
  MarketingButton,
  MarketingCard,
  SectionLabel,
  shell,
} from "./-marketing/MarketingPrimitives";
import {
  EGRESS_NANOS_PER_GIB,
  VOLUME_STORAGE_NANOS_PER_GIB_MONTH,
  CONNECTED_CLOUD_MANAGEMENT_FEE_PERCENT,
  planIds,
  publishedGpuRates,
  NO_CARD_INCLUDED_NANOS,
  NO_CARD_MAX_CONTAINERS,
  publishedPlans,
  publishedShapeRates,
  type PublishedGpuRate,
  type PlanId,
  type PublishedPlan,
} from "./-marketing/pricingCatalog";

export const Route = createLazyFileRoute("/pricing")({
  component: MarketingPricing,
});

const SECONDS_PER_HOUR = 3600;

/** Which unit every figure on the page is currently read in. */
type Meter = "hour" | "second";

/* Every figure the card publishes divides into a whole nanodollar a second, and
   that divisibility is a term of the card rather than a coincidence of these
   numbers. A figure that broke it has no exact per-second price to publish, so
   the page stops instead of quoting a rounded one. */
function perSecond(nanosPerHour: number): number {
  if (nanosPerHour % SECONDS_PER_HOUR !== 0) {
    throw new Error(`${nanosPerHour} nanodollars an hour has no exact per-second price`);
  }
  return nanosPerHour / SECONDS_PER_HOUR;
}

function metered(nanosPerHour: number, meter: Meter): number {
  return meter === "second" ? perSecond(nanosPerHour) : nanosPerHour;
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
  heading: string;
  lines: readonly RateLine[];
};

function perLabel(meter: Meter): string {
  return meter === "second" ? "sec" : "hr";
}

/* Dearest first, ranked once. The page lists cards for one capacity now — what a
   container costs elsewhere is a percentage of these, stated as one line — and
   nothing in the ranking reads the meter, so the toggle cannot change an answer
   fixed when the catalog was generated. */
const fleetGpuRates: readonly PublishedGpuRate[] = [...publishedGpuRates].sort(
  (left, right) => right.nanosPerCardHour.platform_fleet - left.nanosPerCardHour.platform_fleet,
);

/* What a container costs on one kind of capacity: the figures that change with
   where it runs, in the order somebody sizing one asks in. */
function computeGroups(meter: Meter): readonly RateGroup[] {
  const shape = publishedShapeRates.platform_fleet;
  const per = perLabel(meter);
  return [
    {
      heading: "GPU",
      lines: fleetGpuRates.map((rate) => ({
        label: rate.gpuType,
        figure: metered(rate.nanosPerCardHour.platform_fleet, meter),
        unit: `/ ${per}`,
      })),
    },
    {
      heading: "CPU",
      lines: [
        {
          label: "Every core a container holds",
          figure: metered(shape.nanosPerCpuCoreHour, meter),
          unit: `/ core / ${per}`,
        },
      ],
    },
    {
      heading: "Memory",
      lines: [
        {
          label: "Reserved and resident alike",
          figure: metered(shape.nanosPerMemoryGibHour, meter),
          unit: `/ GiB / ${per}`,
        },
      ],
    },
  ];
}

/* What a container moves and keeps, which the card prices once for the platform
   rather than per capacity. Egress is published at a stated zero rather than
   left off, so a reader can tell the traffic is measured and free rather than
   unmeasured. */
const platformGroups: readonly RateGroup[] = [
  {
    heading: "Volumes",
    lines: [
      {
        label: "Kept between runs",
        figure: VOLUME_STORAGE_NANOS_PER_GIB_MONTH,
        /* Thirty days, said rather than implied. Storage meters by the second,
           so a calendar month is charged for the days it actually has — and a
           reader who took "mo" for January would find 31 days on the invoice
           against a figure that quoted 30. */
        unit: "/ GiB / 30 days",
      },
    ],
  },
  {
    heading: "Egress",
    lines: [
      {
        label: "Traffic leaving the platform",
        figure: EGRESS_NANOS_PER_GIB,
        unit: "/ GiB",
      },
    ],
  },
  {
    heading: "Bring your own cloud",
    lines: [
      {
        /* The compute rates, not every rate above it: volumes and egress are this
           platform's own infrastructure and are charged whole wherever a container
           ran. Saying "the rates above" would quietly include them. */
        label: "Management fee on the compute rates above. Your provider bills the machine.",
        figure: `${CONNECTED_CLOUD_MANAGEMENT_FEE_PERCENT}%`,
        unit: "",
      },
    ],
  },
];

const plans: readonly (PublishedPlan & { id: PlanId })[] = planIds.map((id) => ({
  id,
  ...publishedPlans[id],
}));

/* The one account-wide fact a reader needs before choosing a plan: what they get
   before they have paid for anything. The rest is disclosure, not pricing. */
const accountTerm = `Without a card, any plan runs on ${exactDollars(NO_CARD_INCLUDED_NANOS)} of usage and ${NO_CARD_MAX_CONTAINERS} containers. When that is spent, containers stop and new volumes are refused. Volumes you already have stay readable, and keep billing.`;

const sectionTitle =
  "font-serif text-[clamp(1.75rem,4.2vw,2.5rem)] leading-[1.05] font-normal tracking-[-0.005em] text-balance [&_em]:text-brand [&_em]:italic";

/* Both rate lists share one meter, so the two columns are the same column read
   twice rather than two units a reader has to hold at once. */
function MarketingPricing() {
  const [meter, setMeter] = useState<Meter>("hour");
  const fleetRatesId = useId();

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
                A container meters from the second it starts and stops the second it does. You pay
                for the compute you use.
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

            <div className="min-w-0">
              {/* The caption sits under the row rather than beside the heading, so
                  the toggle keeps one position however the line above rewraps. */}
              <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-4">
                <h2 className="font-serif text-[clamp(1.625rem,3vw,2.125rem)] leading-none font-normal">
                  Resource costs
                </h2>
                <MeterToggle controls={fleetRatesId} meter={meter} onChange={setMeter} />
              </div>
              <p className="mt-3.5 text-[12.5px] leading-snug text-muted-foreground">
                On LazyCloud capacity — machines we buy, run, and price whole.
              </p>

              <RateList groups={[...computeGroups(meter), ...platformGroups]} id={fleetRatesId} />
            </div>
          </div>
        </section>

        <section className="border-b border-border bg-muted py-14 sm:py-16 lg:py-20" id="plans">
          <div className={shell}>
            <div className="mb-6 max-w-[44rem]">
              <SectionLabel>Plans</SectionLabel>
              <h2 className={sectionTitle}>Pricing plans</h2>
            </div>
            <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
              {plans.map((plan) => (
                <MarketingCard asChild key={plan.id}>
                  <article className="flex flex-col p-5 sm:p-6">
                    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                      <h3 className="font-serif text-[24px] leading-none font-normal">
                        {plan.name}
                      </h3>
                      <p className="flex items-baseline gap-2">
                        <span className="font-mono text-[22px] leading-none tracking-[-0.02em]">
                          {exactDollars(plan.monthlyNanos)}
                        </span>
                        <span className="text-[12px] text-muted-foreground">per month</span>
                      </p>
                    </div>
                    <p className="mt-3 text-[13px] leading-relaxed text-muted-foreground">
                      {plan.summary}
                    </p>
                    <dl className="mt-4 border-t border-border text-[13px]">
                      <div className="flex items-baseline justify-between gap-4 border-b border-border py-2.5">
                        <dt className="text-muted-foreground">Usage included</dt>
                        <dd className="font-mono font-medium text-brand">
                          {exactDollars(plan.includedNanos)}{" "}
                          <span className="text-muted-foreground">/ month</span>
                        </dd>
                      </div>
                      <div className="flex items-baseline justify-between gap-4 border-b border-border py-2.5">
                        <dt className="text-muted-foreground">Concurrent containers</dt>
                        <dd className="font-mono font-medium">{plan.maxConcurrentContainers}</dd>
                      </div>
                    </dl>
                    <ul className="mt-4 mb-6 grid list-none gap-2 p-0">
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
                    <GetStartedButton className="marketing-action-secondary stamp-quiet mt-auto w-full border-input" />
                  </article>
                </MarketingCard>
              ))}
            </div>

            <p className="mt-4 flex max-w-[58rem] items-start gap-2.5 text-[12.5px] leading-relaxed text-muted-foreground">
              <span className="mt-0.5 shrink-0 text-brand">
                <Glyph>↳</Glyph>
              </span>
              <span>{accountTerm}</span>
            </p>
          </div>
        </section>

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

/* A group's heading sits in its own column beside the first price rather than
   above the block, so the eye runs down one column of resources and one of
   money. */
function RateList({ groups, id }: { groups: readonly RateGroup[]; id: string }) {
  return (
    <dl className="mt-6" id={id}>
      {groups.map((group) => (
        <div
          className="grid grid-cols-[8.5rem_minmax(0,1fr)] gap-x-6 border-t border-border py-5 max-sm:grid-cols-1 max-sm:gap-y-2.5"
          key={group.heading}
        >
          <dt className="text-[13px] font-medium">{group.heading}</dt>
          <dd className="min-w-0">
            {group.lines.map((line) => (
              <p
                className="flex items-baseline justify-between gap-5 py-1.5 first:pt-0"
                key={line.label}
              >
                <span className="min-w-0 text-[13px] leading-snug text-muted-foreground">
                  {line.label}
                </span>
                <span className="shrink-0 font-mono text-[13px] whitespace-nowrap">
                  {typeof line.figure === "number" ? <Rate nanos={line.figure} /> : line.figure}{" "}
                  <span className="text-muted-foreground">{line.unit}</span>
                </span>
              </p>
            ))}
          </dd>
        </div>
      ))}
    </dl>
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

import type { Schemas } from "@/lib/api/client";
import { formatCostNanos } from "@/lib/money";

import { MarketingCard } from "./MarketingPrimitives";

const SECONDS_PER_HOUR = 3600;

export type Meter = "hour" | "second";

function metered(nanosPerHour: number, meter: Meter): number {
  return meter === "hour" ? nanosPerHour : nanosPerHour / SECONDS_PER_HOUR;
}

export type RateLine = {
  label: string;
  figure: number | string;
  unit: string;
  fractionDigits?: number;
  /** Shown muted beside the figure, such as a model the fleet does not run yet. */
  note?: string;
};

export type RateGroup = {
  heading?: string;
  lines: readonly RateLine[];
};

function perLabel(meter: Meter): string {
  return meter === "second" ? "sec" : "hr";
}

/** The platform fleet's GPU, CPU and memory rates of one placement. */
export function computeGroups(
  placement: Schemas["PlacementRate"],
  gpuRates: readonly Schemas["GpuRate"][],
  meter: Meter,
): readonly RateGroup[] {
  const rates = placement.compute_rates.filter((rate) => rate.billing_owner === "platform_fleet");
  const shape = rates.find((rate) => rate.gpu_type === undefined);
  if (!shape) throw new Error("the pricing catalog has no platform fleet rate");
  const enabled = new Set(gpuRates.filter((rate) => rate.enabled).map((rate) => rate.gpu_type));
  const fleetGpuRates = rates
    .flatMap(({ gpu_type: gpuType, ...rate }) => (gpuType ? [{ ...rate, gpuType }] : []))
    .sort((left, right) => right.nanos_per_gpu_card_hour - left.nanos_per_gpu_card_hour);
  const per = perLabel(meter);
  return [
    {
      heading: "GPU",
      lines: fleetGpuRates.map((rate) => ({
        label: rate.gpuType,
        figure: metered(rate.nanos_per_gpu_card_hour, meter),
        unit: `/ ${per}`,
        fractionDigits: meter === "hour" ? 2 : 6,
        note: enabled.has(rate.gpuType) ? undefined : "Coming soon",
      })),
    },
    {
      heading: "CPU and memory",
      lines: [
        {
          label: "CPU",
          figure: metered(shape.nanos_per_cpu_hour, meter),
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

export function RateList({ groups }: { groups: readonly RateGroup[] }) {
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
                  {line.note ? (
                    <span className="mr-2 font-sans text-[12px] text-muted-foreground">
                      {line.note}
                    </span>
                  ) : null}
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

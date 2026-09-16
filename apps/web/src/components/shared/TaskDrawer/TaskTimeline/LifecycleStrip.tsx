import { useState } from "react";
import { type ExecutionPhase, type ExecutionPhaseDomain } from "./phases";
import { axisTicks, elapsedLabel } from "./timeline";
import { formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";
import { AxisLabels } from "./AxisLabels";

export function LifecycleStrip({
  phases,
  domain,
  showLegend = true,
}: {
  phases: ExecutionPhase[];
  domain: ExecutionPhaseDomain;
  showLegend?: boolean;
}) {
  const [activePhaseKey, setActivePhaseKey] = useState<string | null>(null);
  if (!phases.length) return null;
  const ticks = axisTicks(domain, 4);
  const defaultPhase = phases.find((phase) => phase.kind === "execution") ?? phases[0];
  const activePhase = phases.find((phase) => phaseKey(phase) === activePhaseKey) ?? defaultPhase;

  return (
    <div className="min-w-0">
      <div className="flex min-h-7 min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 py-1 text-[11px]">
        <span className={cn("size-2 shrink-0", phaseClass(activePhase))} aria-hidden="true" />
        <span className="min-w-0 truncate text-foreground">{activePhase.label}</span>
        <span className="shrink-0 tabular-nums text-muted-foreground">
          {formatDuration(activePhase.durationMs)} ·{" "}
          {elapsedLabel(activePhase.startMs - domain.startMs)}-
          {elapsedLabel(activePhase.endMs - domain.startMs)}
        </span>
      </div>

      <div
        className="relative h-8 overflow-hidden bg-muted/50"
        aria-label="Task lifecycle timeline"
        onMouseLeave={() => setActivePhaseKey(null)}
      >
        {phases.map((phase) => (
          <button
            key={phaseKey(phase)}
            type="button"
            className={cn(
              "group absolute inset-y-0 min-w-0 border-r border-background/40 outline-none transition-[filter]",
              "hover:brightness-125 focus-visible:z-10 focus-visible:brightness-125 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-foreground/80",
              phaseClass(phase),
            )}
            style={{
              left: `${phase.leftPct}%`,
              width: `${phase.widthPct}%`,
              minWidth: phase.durationMs > 0 ? "2px" : undefined,
            }}
            aria-label={`${phase.label}, ${formatDuration(phase.durationMs)}, ${elapsedLabel(phase.startMs - domain.startMs)} to ${elapsedLabel(phase.endMs - domain.startMs)}`}
            aria-pressed={activePhaseKey === phaseKey(phase)}
            title={`${phase.label}: ${formatDuration(phase.durationMs)}`}
            onMouseEnter={() => setActivePhaseKey(phaseKey(phase))}
            onFocus={() => setActivePhaseKey(phaseKey(phase))}
            onClick={() => setActivePhaseKey(phaseKey(phase))}
          >
            {phase.widthPct >= 18 ? (
              <span
                className={cn(
                  "flex h-full min-w-0 items-center justify-center gap-1 px-1.5 text-[10px]",
                  phase.kind === "execution" ? "text-brand-foreground" : "text-foreground/90",
                )}
              >
                <span className="truncate">{phase.label}</span>
                <span className="shrink-0 tabular-nums opacity-75">
                  {formatDuration(phase.durationMs)}
                </span>
              </span>
            ) : null}
          </button>
        ))}
      </div>

      <div className="relative h-5 border-b border-border/50" aria-label="Elapsed time axis">
        <AxisLabels ticks={ticks} className="top-1" />
      </div>

      {showLegend && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 pt-2 pb-0.5 text-[10px] text-muted-foreground">
          {phases.map((phase) => (
            <button
              key={phaseKey(phase)}
              type="button"
              className="flex shrink-0 items-center gap-1.5 outline-none hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring"
              aria-pressed={activePhaseKey === phaseKey(phase)}
              onMouseEnter={() => setActivePhaseKey(phaseKey(phase))}
              onMouseLeave={() => setActivePhaseKey(null)}
              onFocus={() => setActivePhaseKey(phaseKey(phase))}
              onClick={() => setActivePhaseKey(phaseKey(phase))}
            >
              <span className={cn("size-1.5", phaseClass(phase))} aria-hidden="true" />
              <span>{phase.label}</span>
              <span className="tabular-nums">{formatDuration(phase.durationMs)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function phaseKey(phase: ExecutionPhase): string {
  return `${phase.kind}-${phase.label}-${phase.startMs}`;
}

function phaseClass(phase: ExecutionPhase): string {
  if (phase.kind === "queued") return "bg-muted-foreground/45";
  if (phase.kind === "execution") return "bg-brand";
  return "bg-chart-5";
}

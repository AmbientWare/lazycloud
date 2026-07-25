import { formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

type LifecycleStage = {
  label: string;
  timestamp: string | null | undefined;
  elapsedMs: number | null;
  active: boolean;
};

export function ContainerLifecycle({
  createdAt,
  startedAt,
  finishedAt,
  running,
}: {
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  running: boolean;
}) {
  const stages: LifecycleStage[] = [
    { label: "Created", timestamp: createdAt, elapsedMs: null, active: true },
    {
      label: "Started",
      timestamp: startedAt,
      elapsedMs: differenceMs(createdAt, startedAt),
      active: Boolean(startedAt),
    },
    {
      label: running ? "Running" : "Finished",
      timestamp: running ? null : finishedAt,
      elapsedMs: differenceMs(startedAt, running ? new Date().toISOString() : finishedAt),
      active: running || Boolean(finishedAt),
    },
  ];

  return (
    <div className="overflow-x-auto">
      <ol className="grid min-w-[340px] grid-cols-3" aria-label="Container lifecycle">
        {stages.map((stage, index) => (
          <li key={stage.label} className="relative min-w-0 px-3 pb-1 pt-3 first:pl-0 last:pr-0">
            {index > 0 ? (
              <div
                className={cn(
                  "absolute left-0 right-1/2 top-[18px] h-px",
                  stage.active ? "bg-positive/60" : "bg-border",
                )}
              />
            ) : null}
            {index < stages.length - 1 ? (
              <div
                className={cn(
                  "absolute left-1/2 right-0 top-[18px] h-px",
                  stages[index + 1]?.active ? "bg-positive/60" : "bg-border",
                )}
              />
            ) : null}
            <div className="relative z-10 mx-auto flex w-fit flex-col items-center text-center">
              <span
                className={cn(
                  "mb-2 size-2.5 rounded-full border-2 bg-background",
                  stage.active ? "border-positive" : "border-muted-foreground/40",
                  running && index === stages.length - 1 ? "animate-pulse" : null,
                )}
                aria-hidden="true"
              />
              <span className="text-xs font-medium text-foreground">{stage.label}</span>
              <span
                className="mono mt-0.5 max-w-[6.5rem] truncate text-[11px] tabular-nums text-muted-foreground sm:max-w-none"
                title={stage.timestamp ? exactTime(stage.timestamp) : undefined}
              >
                {stage.timestamp ? exactTime(stage.timestamp) : running ? "Live" : "Not reached"}
              </span>
              {stage.elapsedMs !== null ? (
                <span className="mt-0.5 text-[11px] tabular-nums text-muted-foreground">
                  {index === 1 ? "Queued " : "Elapsed "}
                  {formatDuration(stage.elapsedMs)}
                </span>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

function differenceMs(
  start: string | null | undefined,
  end: string | null | undefined,
): number | null {
  if (!start || !end) return null;
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  if (Number.isNaN(startMs) || Number.isNaN(endMs) || endMs < startMs) return null;
  return endMs - startMs;
}

function exactTime(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime())
    ? value
    : timestamp.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
}

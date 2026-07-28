import type { ContainerLifecycleMetric } from "@/lib/api/schemas";

export type ExecutionPhase = {
  kind: "queued" | "startup" | "execution";
  label: string;
  startMs: number;
  endMs: number;
  durationMs: number;
  leftPct: number;
  widthPct: number;
};

export type ExecutionPhaseDomain = {
  startMs: number;
  endMs: number;
};

type PhaseInput = {
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
};

/** The task request window used by the lifecycle strip. */
export function executionPhaseDomain(task: PhaseInput, nowMs: number): ExecutionPhaseDomain | null {
  const created = parseMs(task.created_at);
  if (created === null) return null;
  const started = parseMs(task.started_at);
  const finished = parseMs(task.finished_at);
  const observedEnd = Math.max(finished ?? nowMs, started ?? created, created);
  return { startMs: created, endMs: Math.max(observedEnd, created + 1_000) };
}

/**
 * User-facing lifecycle rollup on the task request domain. Internal container
 * events determine when preparation began, but are deliberately collapsed to
 * one preparation phase. Events outside this task are excluded so startup work
 * from a reused container is not attributed to the current task.
 */
export function executionPhases(
  task: PhaseInput,
  lifecycle: ContainerLifecycleMetric[],
  nowMs: number,
): ExecutionPhase[] {
  const domain = executionPhaseDomain(task, nowMs);
  if (!domain) return [];
  const started = parseMs(task.started_at);
  const finished = parseMs(task.finished_at);
  const taskEnd = Math.max(finished ?? nowMs, domain.startMs);
  const executionStart = started === null ? null : Math.max(started, domain.startMs);
  const preparationEnd = Math.min(executionStart ?? taskEnd, taskEnd);
  const phases: Array<Omit<ExecutionPhase, "leftPct" | "widthPct">> = [];

  const preparationStarts = lifecycle.flatMap((metric) => {
    const startMs = parseMs(metric.start_time);
    const endMs = parseMs(metric.end_time);
    if (
      startMs === null ||
      endMs === null ||
      endMs <= startMs ||
      endMs <= domain.startMs ||
      startMs >= preparationEnd
    ) {
      return [];
    }
    return [Math.max(startMs, domain.startMs)];
  });
  const preparationStart = preparationStarts.length > 0 ? Math.min(...preparationStarts) : null;

  const queuedEnd = preparationStart ?? preparationEnd;
  if (queuedEnd > domain.startMs) {
    phases.push(phase("queued", "Queued", domain.startMs, queuedEnd));
  }
  if (preparationStart !== null && preparationEnd > preparationStart) {
    phases.push(phase("startup", "Container preparation", preparationStart, preparationEnd));
  }

  if (executionStart !== null && taskEnd > executionStart) {
    phases.push(phase("execution", "Execution", executionStart, taskEnd));
  }

  const spanMs = domain.endMs - domain.startMs;
  return phases.map((entry) => ({
    ...entry,
    leftPct: ((entry.startMs - domain.startMs) / spanMs) * 100,
    widthPct: (entry.durationMs / spanMs) * 100,
  }));
}

function phase(
  kind: ExecutionPhase["kind"],
  label: string,
  startMs: number,
  endMs: number,
): Omit<ExecutionPhase, "leftPct" | "widthPct"> {
  return { kind, label, startMs, endMs, durationMs: endMs - startMs };
}

function parseMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

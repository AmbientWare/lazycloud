import type { Schemas } from "@/lib/api/client";

export type ExecutionPhase = {
  kind: "queued" | "conversion" | "startup" | "execution";
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

/** Stages on the host between placement and readiness: what the timeline calls preparation. */
const PREPARATION: ReadonlySet<Schemas["LifecycleStageKind"]> = new Set([
  "image",
  "source",
  "disk",
  "create",
  "runtime",
]);

/**
 * User-facing lifecycle rollup on the task request domain. A start held for
 * its image conversion shows that wait as its own phase. The container's
 * finished host stages determine when preparation began, but are
 * deliberately collapsed to one preparation phase. Stages outside this task
 * are excluded so startup work from a reused container is not attributed to
 * the current task.
 */
export function executionPhases(
  task: PhaseInput,
  stages: readonly Schemas["LifecycleStage"][],
  nowMs: number,
  timelineDomain?: ExecutionPhaseDomain,
): ExecutionPhase[] {
  const domain = executionPhaseDomain(task, nowMs);
  if (!domain) return [];
  const started = parseMs(task.started_at);
  const finished = parseMs(task.finished_at);
  const taskEnd = Math.max(finished ?? nowMs, domain.startMs);
  const executionStart = started === null ? null : Math.max(started, domain.startMs);
  const preparationEnd = Math.min(executionStart ?? taskEnd, taskEnd);
  const phases: Array<Omit<ExecutionPhase, "leftPct" | "widthPct">> = [];

  const inTask = (included: (kind: Schemas["LifecycleStageKind"]) => boolean) =>
    stages.flatMap((stage) => {
      const startMs = parseMs(stage.started_at);
      const endMs = parseMs(stage.finished_at);
      if (
        !included(stage.stage) ||
        startMs === null ||
        endMs === null ||
        endMs <= startMs ||
        endMs <= domain.startMs ||
        startMs >= preparationEnd
      ) {
        return [];
      }
      return [
        { startMs: Math.max(startMs, domain.startMs), endMs: Math.min(endMs, preparationEnd) },
      ];
    });
  const conversion = inTask((kind) => kind === "conversion")[0] ?? null;
  // Preparation begins when the server sends the start: at the end of a
  // conversion, or else at the first host stage.
  const preparationStarts = inTask((kind) => PREPARATION.has(kind)).map((stage) => stage.startMs);
  if (conversion) preparationStarts.push(conversion.endMs);
  const preparationStart = preparationStarts.length > 0 ? Math.min(...preparationStarts) : null;

  const queuedEnd = conversion?.startMs ?? preparationStart ?? preparationEnd;
  if (queuedEnd > domain.startMs) {
    phases.push(phase("queued", "Queued", domain.startMs, queuedEnd));
  }
  if (conversion && preparationStart !== null && preparationStart > conversion.startMs) {
    phases.push(phase("conversion", "Image conversion", conversion.startMs, preparationStart));
  }
  if (preparationStart !== null && preparationEnd > preparationStart) {
    phases.push(phase("startup", "Container preparation", preparationStart, preparationEnd));
  }

  if (executionStart !== null && taskEnd > executionStart) {
    phases.push(phase("execution", "Execution", executionStart, taskEnd));
  }

  const projection = timelineDomain ?? domain;
  const spanMs = projection.endMs - projection.startMs;
  return phases.map((entry) => ({
    ...entry,
    leftPct: ((entry.startMs - projection.startMs) / spanMs) * 100,
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

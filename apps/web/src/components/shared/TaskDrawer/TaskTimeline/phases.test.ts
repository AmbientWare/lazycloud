import { describe, expect, it } from "vitest";

import type { ContainerLifecycleMetric } from "@/lib/api/schemas";

import { executionPhaseDomain, executionPhases } from "./phases";

const T0 = "2026-07-01T00:00:00Z";
const T5 = "2026-07-01T00:00:05Z";
const T10 = "2026-07-01T00:00:10Z";
const T20 = "2026-07-01T00:00:20Z";
const T40 = "2026-07-01T00:00:40Z";

function metric(
  overrides: Partial<ContainerLifecycleMetric> & { event_id: string },
): ContainerLifecycleMetric {
  return { duration_ms: 0, start_time: null, end_time: null, ...overrides };
}

describe("task execution phase projection", () => {
  it("uses request creation and completion as the public phase domain", () => {
    expect(executionPhaseDomain({ created_at: T0, started_at: T10, finished_at: T40 }, 0)).toEqual({
      startMs: Date.parse(T0),
      endMs: Date.parse(T40),
    });
  });

  it.each([
    {
      name: "completed preparation and execution",
      state: { created_at: T0, started_at: T10, finished_at: T40 },
      metrics: [
        metric({ event_id: "image.load", start_time: T5, end_time: T20, duration_ms: 15_000 }),
      ],
      now: T40,
      expected: [
        ["queued", 5_000],
        ["startup", 5_000],
        ["execution", 30_000],
      ],
    },
    {
      name: "nested lifecycle metrics",
      state: { created_at: T0, started_at: T20, finished_at: T40 },
      metrics: [
        metric({ event_id: "container.startup", start_time: T5, end_time: T20 }),
        metric({ event_id: "image.load", start_time: T10, end_time: "2026-07-01T00:00:15Z" }),
      ],
      now: T40,
      expected: [
        ["queued", 5_000],
        ["startup", 15_000],
        ["execution", 20_000],
      ],
    },
    {
      name: "unfinished execution",
      state: { created_at: T0, started_at: T10, finished_at: null },
      metrics: [],
      now: T40,
      expected: [
        ["queued", 10_000],
        ["execution", 30_000],
      ],
    },
    {
      name: "not-yet-started request",
      state: { created_at: T0, started_at: null, finished_at: null },
      metrics: [],
      now: T20,
      expected: [["queued", 20_000]],
    },
    {
      name: "stale and boundary-free lifecycle metrics",
      state: { created_at: T10, started_at: T20, finished_at: T40 },
      metrics: [
        metric({ event_id: "runner.execution", duration_ms: 5_000 }),
        metric({ event_id: "image.load", start_time: T0, end_time: T5 }),
      ],
      now: T40,
      expected: [
        ["queued", 10_000],
        ["execution", 20_000],
      ],
    },
  ])("projects $name without double counting", ({ state, metrics, now, expected }) => {
    const phases = executionPhases(state, metrics, Date.parse(now));
    expect(phases.map((phase) => [phase.kind, phase.durationMs])).toEqual(expected);
    expect(phases.reduce((total, phase) => total + phase.widthPct, 0)).toBeCloseTo(100, 5);
    expect(phases.map((phase) => phase.label)).not.toContain("Image load");
    expect(phases.map((phase) => phase.label)).not.toContain("Runtime startup");
  });
});

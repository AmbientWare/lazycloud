import { describe, expect, it } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { executionPhaseDomain, executionPhases } from "./phases";

const T0 = "2026-07-01T00:00:00Z";
const T5 = "2026-07-01T00:00:05Z";
const T10 = "2026-07-01T00:00:10Z";
const T20 = "2026-07-01T00:00:20Z";
const T40 = "2026-07-01T00:00:40Z";

function stage(
  kind: Schemas["LifecycleStageKind"],
  started_at: string,
  finished_at?: string,
): Schemas["LifecycleStage"] {
  return { stage: kind, started_at, finished_at };
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
      stages: [stage("image", T5, T20)],
      now: T40,
      expected: [
        ["queued", 5_000],
        ["startup", 5_000],
        ["execution", 30_000],
      ],
    },
    {
      name: "several start stages",
      state: { created_at: T0, started_at: T20, finished_at: T40 },
      stages: [
        stage("placement", T0, T5),
        stage("image", T5, T10),
        stage("create", T10, "2026-07-01T00:00:15Z"),
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
      state: { created_at: T0, started_at: T10 },
      stages: [],
      now: T40,
      expected: [
        ["queued", 10_000],
        ["execution", 30_000],
      ],
    },
    {
      name: "not-yet-started request",
      state: { created_at: T0 },
      stages: [],
      now: T20,
      expected: [["queued", 20_000]],
    },
    {
      name: "running and earlier start stages",
      state: { created_at: T10, started_at: T20, finished_at: T40 },
      stages: [stage("runtime", T10), stage("image", T0, T5)],
      now: T40,
      expected: [
        ["queued", 10_000],
        ["execution", 20_000],
      ],
    },
  ])("projects $name without double counting", ({ state, stages, now, expected }) => {
    const phases = executionPhases(state, stages, Date.parse(now));
    expect(phases.map((phase) => [phase.kind, phase.durationMs])).toEqual(expected);
    expect(phases.reduce((total, phase) => total + phase.widthPct, 0)).toBeCloseTo(100, 5);
  });
});

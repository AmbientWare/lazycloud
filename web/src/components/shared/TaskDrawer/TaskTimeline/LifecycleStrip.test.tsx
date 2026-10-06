import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";

import { LifecycleStrip } from "./LifecycleStrip";
import { executionPhaseDomain, executionPhases } from "./phases";

it("shows a start's wait for its image conversion as its own stage, not as queued", () => {
  const task = {
    created_at: "2026-07-01T00:00:00Z",
    started_at: "2026-07-01T00:00:12Z",
    finished_at: "2026-07-01T00:00:20Z",
  };
  const stages = [
    {
      stage: "placement" as const,
      started_at: "2026-07-01T00:00:00Z",
      finished_at: "2026-07-01T00:00:00.100Z",
    },
    {
      stage: "conversion" as const,
      started_at: "2026-07-01T00:00:00.100Z",
      finished_at: "2026-07-01T00:00:09Z",
    },
    {
      stage: "image" as const,
      started_at: "2026-07-01T00:00:09.200Z",
      finished_at: "2026-07-01T00:00:10Z",
      cached: false,
    },
  ];
  const nowMs = Date.parse(task.finished_at);
  const domain = executionPhaseDomain(task, nowMs)!;
  render(<LifecycleStrip phases={executionPhases(task, stages, nowMs)} domain={domain} />);

  const bar = screen.getByLabelText("Task lifecycle timeline");
  expect(
    within(bar)
      .getAllByRole("button")
      .map((phase) => phase.title),
  ).toEqual([
    "Queued: 100ms",
    "Image conversion: 8.9s",
    "Container preparation: 3.0s",
    "Execution: 8.0s",
  ]);
  expect(within(bar).getByTitle("Image conversion: 8.9s")).toHaveClass("bg-chart-3");
});

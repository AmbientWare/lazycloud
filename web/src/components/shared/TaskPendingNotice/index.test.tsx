import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { TaskPendingNotice } from ".";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it("shows a delayed pending notice and clears it when execution starts", () => {
  vi.useFakeTimers();
  const now = new Date();
  vi.setSystemTime(now);
  const task: Schemas["Task"] = {
    id: "task",
    app: "media",
    function: "transcribe",
    release_id: "release",
    status: "queued",
    attempts: 0,
    max_attempts: 1,
    root_task_id: "task",
    created_at: now.toISOString(),
    pending: {
      reason: "provisioning_compute",
      message: "Compute progress from the server",
      since: now.toISOString(),
      pending_since: now.toISOString(),
      observed_at: now.toISOString(),
    },
  };
  const view = render(<TaskPendingNotice task={task} />);
  expect(screen.queryByRole("status")).toBeNull();
  act(() => vi.advanceTimersByTime(5_000));
  expect(screen.getByRole("status")).toBeVisible();
  view.rerender(<TaskPendingNotice task={{ ...task, status: "running" }} />);
  expect(screen.queryByRole("status")).toBeNull();
});

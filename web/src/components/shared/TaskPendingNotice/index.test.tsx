import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { taskSchema } from "@/lib/api/schemas/tasks";
import { TaskPendingNotice } from ".";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it("shows a delayed pending notice and clears it when execution starts", () => {
  vi.useFakeTimers();
  const now = new Date();
  vi.setSystemTime(now);
  const task = taskSchema.parse({
    id: "task",
    name: "transcribe",
    status: "pending",
    created_at: now.toISOString(),
    pending_progress: {
      reason: "provisioning_compute",
      message: "Compute progress from the server",
      since: now.toISOString(),
      pending_since: now.toISOString(),
      observed_at: now.toISOString(),
    },
  });
  const view = render(<TaskPendingNotice task={task} />);
  expect(screen.queryByRole("status")).toBeNull();
  act(() => vi.advanceTimersByTime(5_000));
  expect(screen.getByRole("status")).toBeVisible();
  view.rerender(<TaskPendingNotice task={{ ...task, status: "running" }} />);
  expect(screen.queryByRole("status")).toBeNull();
});

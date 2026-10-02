import { QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { testQueryClient } from "@/test/query-client";

import { UsageRows } from "./UsageRows";

const window = { start: "2026-07-21T00:00:00Z", end: "2026-07-22T00:00:00Z" };

function row(fields: Partial<Schemas["UsageCostRow"]>): Schemas["UsageCostRow"] {
  return {
    workspace_id: "workspace-1",
    workspace_name: "dev",
    app_id: "app-1",
    app_name: "reports",
    workload_id: "workload-1",
    workload_name: "summarize",
    workload_kind: "function",
    cost_nanos: 3_000_000,
    components: [],
    ...fields,
  };
}

it("lists a workload's runs with View run when its row opens", async () => {
  const requests: URL[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL((input as Request).url);
    requests.push(url);
    const runs = url.searchParams.get("group_by") === "task";
    return Response.json({
      start: window.start,
      end: window.end,
      currency: "USD",
      group_by: runs ? "task" : "workload",
      cost_nanos: 3_000_000,
      rows: runs
        ? [
            row({ task_id: "01a0f9ec-4273-79b4-a153-7a121e5d034c", cost_nanos: 2_000_000 }),
            row({ cost_nanos: 1_000_000 }),
          ]
        : [row({})],
    });
  });
  const root = createRootRoute({
    component: () => (
      <UsageRows window={window} scope={{ groupBy: "workload", appId: "app-1" }} currency="USD" />
    ),
  });
  const router = createRouter({
    routeTree: root,
    history: createMemoryHistory({ initialEntries: ["/w/dev/usage"] }),
  });
  render(
    <QueryClientProvider client={testQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await act(() => router.load());

  // The workload stays one row, as the reference showed it.
  const workload = await screen.findByText("summarize");
  expect(screen.queryByText("View run")).toBeNull();
  const details = workload.closest("details");
  if (!details) throw new Error("the workload row is not expandable");
  details.open = true;
  fireEvent(details, new Event("toggle"));

  expect(await screen.findByText("Run 1e5d034c")).toBeVisible();
  expect(screen.getByText("View run")).toHaveAttribute(
    "href",
    "/w/dev/tasks/01a0f9ec-4273-79b4-a153-7a121e5d034c",
  );
  const asked = requests.find((url) => url.searchParams.get("group_by") === "task");
  expect(asked?.searchParams.get("workload_id")).toBe("workload-1");
});

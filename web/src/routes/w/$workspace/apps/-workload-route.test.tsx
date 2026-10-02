import { QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { WorkspaceContext } from "@/lib/workspace-context";
import { testQueryClient } from "@/test/query-client";

import { Route as WorkloadRoute } from "./$app_.workloads.$kind.$name";

const workspace: Schemas["Workspace"] = {
  id: "workspace-1",
  name: "dev",
  state: "active",
  role: "owner",
  created_at: "2026-07-21T10:00:00Z",
};

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

it("calls a kind the API does not define not found without asking the server", async () => {
  await renderWorkload("/w/dev/apps/reports/workloads/devbox/box");

  expect(await screen.findByText("No deployed workload named box in this app")).toBeVisible();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("calls a workload the server does not deploy not found", async () => {
  fetchMock.mockImplementation(async () =>
    Response.json({ code: "not_found", message: "not found" }, { status: 404 }),
  );
  await renderWorkload("/w/dev/apps/reports/workloads/pod/box");

  expect(await screen.findByText("No deployed workload named box in this app")).toBeVisible();
  const url = new URL((fetchMock.mock.calls[0]?.[0] as Request).url);
  expect(url.pathname).toBe("/v1/workspaces/dev/apps/reports/workloads/pod/box");
});

async function renderWorkload(path: string) {
  const root = createRootRoute({ component: Outlet });
  const layout = createRoute({
    getParentRoute: () => root,
    path: "/w/$workspace",
    component: () => (
      <WorkspaceContext.Provider value={{ workspace, workspaces: [workspace] }}>
        <Outlet />
      </WorkspaceContext.Provider>
    ),
  });
  // As the generated route tree attaches it.
  const page = WorkloadRoute.update({
    id: "/apps/$app_/workloads/$kind/$name",
    path: "/apps/$app/workloads/$kind/$name",
    getParentRoute: () => layout,
  } as never);
  const router = createRouter({
    routeTree: root.addChildren([layout.addChildren([page])]),
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <QueryClientProvider client={testQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await act(() => router.load());
}

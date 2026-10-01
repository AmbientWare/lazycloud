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
import { WorkspaceContext } from "@/lib/workspace-context";
import { testQueryClient } from "@/test/query-client";

import { GlobalSearch } from "./GlobalSearch";

const workspace: Schemas["Workspace"] = {
  id: "workspace-1",
  name: "dev",
  state: "active",
  role: "owner",
  created_at: "2026-07-21T10:00:00Z",
};

it("asks the server for matching workloads and tasks", async () => {
  // cmdk measures its list; jsdom has no layout to observe.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = () => undefined;
  const requests: URL[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL((input as Request).url);
    requests.push(url);
    if (url.pathname.endsWith("/apps/greet")) {
      return Response.json({ code: "not_found", message: "no app" }, { status: 404 });
    }
    if (url.pathname.endsWith("/deployments")) {
      const named = url.searchParams.get("name") === "greet";
      return Response.json({
        deployments: named
          ? [
              {
                id: "workload-1",
                app: "journey",
                name: "greet",
                kind: "function",
                state: "active",
                created_at: "2026-07-21T10:00:00Z",
              },
            ]
          : [],
      });
    }
    if (url.pathname.endsWith("/tasks")) {
      return Response.json({
        tasks: url.searchParams.get("search") === "greet" ? [task("4f1c2d3e-0000")] : [],
      });
    }
    return Response.json({ apps: [] });
  });
  const root = createRootRoute({
    component: () => (
      <WorkspaceContext.Provider value={{ workspace, workspaces: [workspace] }}>
        <GlobalSearch open onOpenChange={() => undefined} />
      </WorkspaceContext.Provider>
    ),
  });
  const router = createRouter({
    routeTree: root,
    history: createMemoryHistory({ initialEntries: ["/w/dev/apps"] }),
  });
  render(
    <QueryClientProvider client={testQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await act(() => router.load());

  fireEvent.change(await screen.findByPlaceholderText("Search workspace…"), {
    target: { value: "greet" },
  });

  expect(await screen.findByText("Function")).toBeVisible();
  expect(await screen.findByText(/^Running · 4f1c2d3e$/)).toBeVisible();
  expect(requests.some((url) => url.searchParams.get("search") === "greet")).toBe(true);
  expect(requests.some((url) => url.searchParams.get("name") === "greet")).toBe(true);
});

function task(id: string): Schemas["Task"] {
  return {
    id,
    app: "journey",
    function: "greet",
    release_id: "release-1",
    status: "running",
    attempts: 1,
    max_attempts: 1,
    root_task_id: id,
    created_at: "2026-07-21T10:00:00Z",
  };
}

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

it("asks the server for matching apps, workloads, tasks and sandboxes", async () => {
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
    if (url.pathname.endsWith("/workloads")) {
      const named = url.searchParams.get("search") === "greet";
      return Response.json({
        workloads: named
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
        tasks:
          url.searchParams.get("search") === "greet"
            ? [task("01a0f9ec-4273-79b4-a153-7a121e5d034c")]
            : [],
      });
    }
    if (url.pathname.endsWith("/sandboxes")) {
      return Response.json({
        sandboxes:
          url.searchParams.get("search") === "greet"
            ? [
                {
                  id: "01a0f9ec-4273-79b4-a153-7a121e5dbox1",
                  release_id: "release-2",
                  app: "journey",
                  name: "greeter-box",
                  status: "running",
                  gpu: [],
                  created_at: "2026-07-21T10:00:00Z",
                },
              ]
            : [],
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
  expect(await screen.findByText(/^Running · 1e5d034c$/)).toBeVisible();
  expect(await screen.findByText("greeter-box")).toBeVisible();
  const searched = requests
    .filter((url) => url.searchParams.get("search") === "greet")
    .map((url) => url.pathname.split("/").at(-1));
  expect([...new Set(searched)].sort()).toEqual(["apps", "sandboxes", "tasks", "workloads"]);

  // The query filtered out the highlighted "Apps" destination, so the first
  // remaining result is highlighted and Enter opens it.
  const highlighted = document.querySelector('[cmdk-item][data-selected="true"]');
  expect(highlighted?.textContent).toContain("greet");
  fireEvent.keyDown(screen.getByPlaceholderText("Search workspace…"), { key: "Enter" });
  await vi.waitFor(() =>
    expect(router.state.location.pathname).toBe("/w/dev/apps/journey/workloads/function/greet"),
  );
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

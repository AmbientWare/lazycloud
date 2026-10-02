import { QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { clearStoredAuthToken, setStoredAuthToken } from "@/lib/auth";
import { useWorkspace } from "@/lib/workspace-context";
import { testQueryClient } from "@/test/query-client";

import { Route as WorkspaceRoute } from "./route";

vi.mock("@/components/shared/AppShell", () => ({
  AppShell: function Shell() {
    return <p>Shell for {useWorkspace().workspace.name}</p>;
  },
}));
vi.mock("@/components/shared/WorkspaceLiveUpdates", () => ({
  WorkspaceLiveUpdatesProvider: ({ children }: { children: ReactNode }) => children,
}));

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  setStoredAuthToken("session");
});

afterEach(() => {
  clearStoredAuthToken();
});

it("re-reads the session once for a workspace it does not list yet", async () => {
  fetchMock
    .mockResolvedValueOnce(Response.json(me([workspace("dev")])))
    .mockResolvedValueOnce(Response.json(me([workspace("dev"), workspace("research")])));
  await renderWorkspace("research");

  expect(await screen.findByText("Shell for research")).toBeVisible();
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("calls a workspace not found after one re-read still lacks it", async () => {
  fetchMock.mockImplementation(async () => Response.json(me([workspace("dev")])));
  await renderWorkspace("research");

  expect(await screen.findByText("Workspace not found")).toBeVisible();
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("link", { name: "dev" })).toBeVisible();
});

async function renderWorkspace(name: string) {
  const root = createRootRoute({ component: Outlet });
  const layout = createRoute({
    getParentRoute: () => root,
    path: "/w/$workspace",
    component: WorkspaceRoute.options.component,
  });
  const apps = createRoute({ getParentRoute: () => layout, path: "/apps" });
  const router = createRouter({
    routeTree: root.addChildren([layout.addChildren([apps])]),
    history: createMemoryHistory({ initialEntries: [`/w/${name}`] }),
  });
  render(
    <QueryClientProvider client={testQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await act(() => router.load());
}

function me(workspaces: Schemas["Workspace"][]): Schemas["Me"] {
  return {
    user: {
      id: "user-1",
      display_name: "owner",
      email: "",
      avatar_url: "",
      github_login: "",
      is_admin: false,
      status: "active",
      created_at: "2026-07-21T10:00:00Z",
    },
    workspaces,
  };
}

function workspace(name: string): Schemas["Workspace"] {
  return {
    id: `id-${name}`,
    name,
    state: "active",
    role: "owner",
    created_at: "2026-07-21T10:00:00Z",
  };
}

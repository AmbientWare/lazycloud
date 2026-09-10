import { testQueryClient } from "@/test/query-client";
import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { getStoredAuthToken, setStoredAuthToken, clearStoredAuthToken } from "@/lib/auth";
import type { CurrentSession } from "@/lib/api/schemas";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import { Route as CallbackRoute } from "@/routes/callback";

import { AuthGate } from ".";
import { useSession } from "./session";

let queryClient: QueryClient;
const fetchMock = vi.fn<typeof fetch>();
const session: CurrentSession = {
  user: {
    id: "user-1",
    display_name: "owner",
    email: "",
    avatar_url: "",
    github_user_id: "",
    github_login: "",
    role: "member",
    status: "active",
    created_at: "2026-07-21T10:00:00Z",
    updated_at: "2026-07-21T10:00:00Z",
  },
  workspaces: [],
};

beforeEach(() => {
  queryClient = testQueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 15_000 } },
  });
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  setStoredAuthToken("test-session");
});

afterEach(() => {
  clearStoredAuthToken();
  window.history.replaceState(null, "", "/");
});

it("retries a session outage in place without requiring another sign-in", async () => {
  fetchMock.mockResolvedValueOnce(new Response("Unavailable", { status: 503 }));
  const router = await renderSession();
  await screen.findByRole("button", { name: "Retry" });
  expect(screen.queryByRole("link", { name: "Continue with GitHub" })).not.toBeInTheDocument();
  expect(getStoredAuthToken()).toBe("test-session");

  fetchMock.mockResolvedValueOnce(Response.json(session));
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByText("Account user-1");
  expect(router.state.location.href).toBe("/dashboard?settings=workspace#logs");
});

it("returns an expired session to sign-in with its full destination", async () => {
  fetchMock.mockResolvedValueOnce(new Response("Session expired", { status: 401 }));
  await renderSession();
  const link = await screen.findByRole("link", { name: "Continue with GitHub" });
  expect(getStoredAuthToken()).toBeNull();
  expect(
    new URL(link.getAttribute("href")!, "http://localhost").searchParams.get("return_to"),
  ).toBe("/dashboard?settings=workspace#logs");
});

it("keeps authorization failures distinct from an expired session", async () => {
  fetchMock.mockResolvedValueOnce(new Response("Account access is disabled", { status: 403 }));
  await renderSession();
  await screen.findByText("Account access is disabled");
  expect(getStoredAuthToken()).toBe("test-session");
  expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
  expect(screen.queryByRole("link", { name: "Continue with GitHub" })).not.toBeInTheDocument();
});

it("redeems a callback once under StrictMode and clears the previous account cache", async () => {
  queryClient.setQueryData(currentSessionQueryOptions().queryKey, {
    ...session,
    user: { ...session.user, id: "previous-user" },
  });
  queryClient.setQueryData(["apps", "previous-workspace"], { private: true });
  window.history.replaceState(null, "", "/callback#code=test-exchange-code");
  fetchMock.mockResolvedValueOnce(
    Response.json({
      token: "new-test-session",
      expires_at: "2026-07-22T10:00:00Z",
      user: session.user,
      return_to: "/dashboard?settings=workspace#logs",
    }),
  );
  fetchMock.mockResolvedValueOnce(Response.json(session));
  const router = await renderSession("/callback#code=test-exchange-code");

  await screen.findByText("Account user-1");
  expect(queryClient.getQueryData(["apps", "previous-workspace"])).toBeUndefined();
  expect(getStoredAuthToken()).toBe("new-test-session");
  expect(window.location.hash).toBe("");
  expect(router.state.location.href).toBe("/dashboard?settings=workspace#logs");
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

async function renderSession(path = "/dashboard?settings=workspace#logs") {
  const root = createRootRoute({
    component: Outlet,
  });
  function AccountPage() {
    return <p>Account {useSession().user.id}</p>;
  }
  const dashboard = createRoute({
    getParentRoute: () => root,
    path: "/dashboard",
    component: () => (
      <AuthGate>
        <AccountPage />
      </AuthGate>
    ),
  });
  const callback = createRoute({
    getParentRoute: () => root,
    path: "/callback",
    component: CallbackRoute.options.component,
  });
  const router = createRouter({
    routeTree: root.addChildren([dashboard, callback]),
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </StrictMode>,
  );
  await act(() => router.load());
  await waitFor(() => expect(router.state.isLoading).toBe(false));
  return router;
}

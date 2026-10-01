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
import type { Schemas } from "@/lib/api/client";
import { currentSessionQueryOptions, SESSION_MARKER } from "@/lib/queries/auth";
import { Route as CallbackRoute } from "@/routes/callback";

import { AuthGate } from ".";
import { useSession } from "./session";

let queryClient: QueryClient;
const fetchMock = vi.fn<typeof fetch>();
// GET /v1/me: who the session cookie belongs to.
const session: Schemas["Me"] = {
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
  // The server's callback returns the browser to /callback, whose fragment
  // names the full destination.
  expect(
    new URL(link.getAttribute("href")!, "http://localhost").searchParams.get("return_to"),
  ).toBe(`/callback#code=${encodeURIComponent("/dashboard?settings=workspace#logs")}`);
});

it("keeps authorization failures distinct from an expired session", async () => {
  fetchMock.mockResolvedValueOnce(
    Response.json({ code: "forbidden", message: "Account access is disabled" }, { status: 403 }),
  );
  await renderSession();
  await screen.findByText("Account access is disabled");
  expect(getStoredAuthToken()).toBe("test-session");
  expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
  expect(screen.queryByRole("link", { name: "Continue with GitHub" })).not.toBeInTheDocument();
});

it("redeems a callback once under StrictMode and clears the previous account cache", async () => {
  queryClient.setQueryData(currentSessionQueryOptions().queryKey, {
    user: { ...session.user, id: "previous-user" },
    workspaces: [],
  });
  queryClient.setQueryData(["apps", "previous-workspace"], { private: true });
  clearStoredAuthToken();
  const callback = `/callback#code=${encodeURIComponent("/dashboard?settings=workspace#logs")}`;
  window.history.replaceState(null, "", callback);
  // The read that confirms the cookie the server set is also the shell's session.
  fetchMock.mockImplementation(async () => Response.json(session));
  const router = await renderSession(callback);

  await screen.findByText("Account user-1");
  expect(queryClient.getQueryData(["apps", "previous-workspace"])).toBeUndefined();
  expect(getStoredAuthToken()).toBe(SESSION_MARKER);
  expect(window.location.hash).toBe("");
  expect(router.state.location.href).toBe("/dashboard?settings=workspace#logs");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("decodes the callback destination once", async () => {
  clearStoredAuthToken();
  // The destination's own query holds the literal text `%41`. Decoding it twice
  // would turn that into `A`.
  const callback = `/callback#code=${encodeURIComponent("/dashboard?q=%2541")}`;
  window.history.replaceState(null, "", callback);
  fetchMock.mockImplementation(async () => Response.json(session));
  const router = await renderSession(callback);

  await screen.findByText("Account user-1");
  expect(router.state.location.search).toEqual({ q: "%41" });
});

it("signs out by ending the session and clearing every cached answer", async () => {
  fetchMock.mockImplementation(async (input) =>
    (input as Request).method === "DELETE"
      ? new Response(null, { status: 204 })
      : Response.json(session),
  );
  const router = await renderSession("/dashboard");
  await screen.findByText("Account user-1");
  queryClient.setQueryData(["workspace", "dev", "apps"], ["journey"]);

  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

  await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  expect(getStoredAuthToken()).toBeNull();
  expect(queryClient.getQueryData(["workspace", "dev", "apps"])).toBeUndefined();
  expect(queryClient.getQueryData(currentSessionQueryOptions().queryKey)).toBeUndefined();
  const ended = fetchMock.mock.calls.map(([input]) => input as Request);
  expect(ended.some((request) => request.method === "DELETE")).toBe(true);
});

async function renderSession(path = "/dashboard?settings=workspace#logs") {
  const root = createRootRoute({
    component: Outlet,
  });
  function AccountPage() {
    const { user, logout } = useSession();
    return (
      <>
        <p>Account {user.id}</p>
        <button type="button" onClick={logout}>
          Sign out
        </button>
      </>
    );
  }
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => "Home" });
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
    routeTree: root.addChildren([home, dashboard, callback]),
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

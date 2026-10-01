import { testQueryClient } from "@/test/query-client";
import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { useAdminSettingsController } from "./controller";

type ApiUser = Schemas["User"];

describe("admin settings controller", () => {
  it("sends nothing for a demotion or a disable until it is confirmed", async () => {
    const other = account("user-2", { is_admin: true });
    const requests = recordRequests([account("user-1"), other]);
    const { result } = await mountedController("user-1");

    const listed = result.current.accounts.find((row) => row.user.id === "user-2")!;
    act(() => result.current.setRole(listed, "member"));
    expect(result.current.confirming?.action).toBe("demote");
    expect(requests.puts()).toEqual([]);
    act(() => result.current.confirm());
    await waitFor(() => expect(result.current.confirming).toBeNull());
    expect(requests.puts()).toEqual([{ path: "/v1/users/user-2/role", body: { role: "member" } }]);

    const demoted = result.current.accounts.find((row) => row.user.id === "user-2")!;
    expect(demoted.user.role).toBe("member");
    act(() => result.current.toggleStatus(demoted));
    expect(result.current.confirming?.action).toBe("disable");
    act(() => result.current.confirm());
    await waitFor(() => expect(result.current.confirming).toBeNull());
    expect(requests.puts().at(-1)).toEqual({
      path: "/v1/users/user-2/status",
      body: { status: "disabled" },
    });

    const disabled = result.current.accounts.find((row) => row.user.id === "user-2")!;
    expect(disabled.user.status).toBe("disabled");
    expect(requests.puts()).toHaveLength(2);
  });

  it("refuses to demote or disable the acting administrator", async () => {
    const requests = recordRequests([account("user-1", { is_admin: true })]);
    const { result } = await mountedController("user-1");
    const self = result.current.accounts[0]!;

    act(() => result.current.setRole(self, "member"));
    act(() => result.current.toggleStatus(self));

    expect(result.current.confirming).toBeNull();
    expect(requests.puts()).toEqual([]);
  });
});

async function mountedController(actingUserId: string) {
  const rendered = renderHook(() => useAdminSettingsController({ actingUserId }), {
    wrapper: wrapper(testQueryClient()),
  });
  await waitFor(() => expect(rendered.result.current.accounts.length).toBeGreaterThan(0));
  return rendered;
}

function wrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

/**
 * Every call the controller makes, answered the way the server would: the list
 * and a role or status change answer with the API's users.
 */
function recordRequests(accounts: ApiUser[]) {
  const sent: { path: string; method: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", async (request: Request) => {
    const path = new URL(request.url).pathname;
    const text = await request.text();
    const body: unknown = text ? JSON.parse(text) : undefined;
    sent.push({ path, method: request.method, body });
    const role = path.match(/^\/v1\/users\/([^/]+)\/role$/);
    if (role) {
      const row = rowFor(accounts, role[1]!);
      return jsonResponse({
        ...row,
        is_admin: (body as { role: string }).role === "administrator",
      });
    }
    const status = path.match(/^\/v1\/users\/([^/]+)\/status$/);
    if (status) {
      const row = rowFor(accounts, status[1]!);
      return jsonResponse({ ...row, status: (body as { status: ApiUser["status"] }).status });
    }
    return jsonResponse({ users: accounts });
  });
  return {
    puts: () =>
      sent
        .filter((call) => call.method === "PUT")
        .map((call) => ({ path: call.path, body: call.body })),
  };
}

function rowFor(accounts: ApiUser[], userId: string): ApiUser {
  const row = accounts.find((candidate) => candidate.id === userId);
  if (!row) throw new Error(`no fixture for ${userId}`);
  return row;
}

function account(id: string, overrides: Partial<ApiUser> = {}): ApiUser {
  return {
    id,
    display_name: id,
    email: `${id}@example.com`,
    avatar_url: "",
    github_login: id,
    is_admin: false,
    status: "active",
    created_at: "2026-07-21T10:00:00Z",
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

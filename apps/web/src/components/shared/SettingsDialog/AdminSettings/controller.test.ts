import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BillingAccountAdmin, User } from "@/lib/api/schemas";

import { useAdminSettingsController } from "./controller";

afterEach(() => vi.restoreAllMocks());

describe("admin settings controller", () => {
  it("sends nothing for a demotion, a disable, or a revoke until it is confirmed", async () => {
    const other = account("user-2", {
      role: "administrator",
      complimentary_since: "2026-08-01T00:00:00Z",
    });
    const requests = recordRequests([account("user-1"), other]);
    const { result } = await mountedController("user-1");

    act(() => result.current.setRole(other, "member"));
    expect(result.current.confirming?.action).toBe("demote");
    expect(requests.puts()).toEqual([]);
    act(() => result.current.confirm());
    await waitFor(() => expect(result.current.confirming).toBeNull());
    expect(requests.puts()).toEqual([
      { path: "/api/v1/users/user-2/role", body: { role: "member" } },
    ]);

    const demoted = result.current.accounts.find((row) => row.user.id === "user-2")!;
    expect(demoted.user.role).toBe("member");
    act(() => result.current.toggleStatus(demoted));
    expect(result.current.confirming?.action).toBe("disable");
    act(() => result.current.confirm());
    await waitFor(() => expect(result.current.confirming).toBeNull());
    expect(requests.puts().at(-1)).toEqual({
      path: "/api/v1/users/user-2/status",
      body: { status: "disabled" },
    });

    const disabled = result.current.accounts.find((row) => row.user.id === "user-2")!;
    expect(disabled.user.status).toBe("disabled");
    act(() => result.current.toggleComplimentary(disabled));
    expect(result.current.confirming?.action).toBe("revoke");
    act(() => result.current.confirm());
    await waitFor(() => expect(result.current.confirming).toBeNull());
    expect(requests.puts().at(-1)).toEqual({
      path: "/api/v1/billing/accounts/user-2/complimentary",
      body: { complimentary: false },
    });
    expect(requests.puts()).toHaveLength(3);
    expect(
      result.current.accounts.find((row) => row.user.id === "user-2")?.complimentary_since,
    ).toBeNull();
  });

  it("refuses to demote or disable the acting administrator", async () => {
    const self = account("user-1", { role: "administrator" });
    const requests = recordRequests([self]);
    const { result } = await mountedController("user-1");

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

function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

/**
 * Every call the controller makes, answered the way the server would: a role or
 * status change returns the user, a waiver change returns the whole row.
 */
function recordRequests(accounts: BillingAccountAdmin[]) {
  const sent: { path: string; method: string; body: unknown }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = new URL(String(input), "http://dashboard.example").pathname;
    const method = init?.method ?? "GET";
    const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    sent.push({ path, method, body });
    const role = path.match(/^\/api\/v1\/users\/([^/]+)\/role$/);
    if (role) {
      const row = rowFor(accounts, role[1]!);
      return jsonResponse({ ...row.user, role: (body as { role: User["role"] }).role });
    }
    const status = path.match(/^\/api\/v1\/users\/([^/]+)\/status$/);
    if (status) {
      const row = rowFor(accounts, status[1]!);
      return jsonResponse({ ...row.user, status: (body as { status: User["status"] }).status });
    }
    const waiver = path.match(/^\/api\/v1\/billing\/accounts\/([^/]+)\/complimentary$/);
    if (waiver) {
      const row = rowFor(accounts, waiver[1]!);
      const granted = (body as { complimentary: boolean }).complimentary;
      return jsonResponse({
        ...row,
        complimentary_since: granted ? "2026-09-03T00:00:00Z" : null,
      });
    }
    return jsonResponse({ data: accounts, next: "" });
  });
  return {
    puts: () =>
      sent
        .filter((call) => call.method === "PUT")
        .map((call) => ({ path: call.path, body: call.body })),
  };
}

function rowFor(accounts: BillingAccountAdmin[], userId: string): BillingAccountAdmin {
  const row = accounts.find((candidate) => candidate.user.id === userId);
  if (!row) throw new Error(`no fixture for ${userId}`);
  return row;
}

function account(
  id: string,
  overrides: Partial<User> & Pick<Partial<BillingAccountAdmin>, "complimentary_since"> = {},
): BillingAccountAdmin {
  const { complimentary_since = null, ...user } = overrides;
  return {
    user: {
      id,
      display_name: id,
      email: `${id}@example.com`,
      avatar_url: "",
      github_user_id: "",
      github_login: id,
      role: "member",
      status: "active",
      created_at: "2026-07-21T10:00:00Z",
      updated_at: "2026-07-21T10:00:00Z",
      ...user,
    },
    status: "active",
    plan: "free",
    payment_method_on_file: false,
    complimentary_since,
    recent_cost_nanos: 0,
    recent_cost_since: "2026-08-04T00:00:00Z",
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

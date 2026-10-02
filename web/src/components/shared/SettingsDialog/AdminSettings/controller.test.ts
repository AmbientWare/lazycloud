import { testQueryClient } from "@/test/query-client";
import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { useAdminSettingsController } from "./controller";

type ApiAccount = Schemas["BillingAccountAdmin"];
type ApiUser = Schemas["User"];

describe("admin settings controller", () => {
  it("sends nothing for a demotion, a disable, or a revoke until it is confirmed", async () => {
    const other = account("user-2", { is_admin: true }, "2026-08-01T00:00:00Z");
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
    expect(demoted.user.is_admin).toBe(false);
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
    act(() => result.current.toggleComplimentary(disabled));
    expect(result.current.confirming?.action).toBe("revoke");
    act(() => result.current.confirm());
    await waitFor(() => expect(result.current.confirming).toBeNull());
    expect(requests.puts().at(-1)).toEqual({
      path: "/v1/billing/accounts/user-2/complimentary",
      body: { complimentary: false },
    });
    expect(requests.puts()).toHaveLength(3);
    expect(
      result.current.accounts.find((row) => row.user.id === "user-2")?.complimentary_since,
    ).toBeUndefined();
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
 * Every call the controller makes, answered the way the server would: a role or
 * status change returns the user, a waiver change returns the whole account.
 */
function recordRequests(accounts: ApiAccount[]) {
  const sent: { path: string; method: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", async (request: Request) => {
    const path = new URL(request.url).pathname;
    const text = await request.text();
    const body: unknown = text ? JSON.parse(text) : undefined;
    sent.push({ path, method: request.method, body });
    const role = path.match(/^\/v1\/users\/([^/]+)\/role$/);
    if (role) {
      const { user } = rowFor(accounts, role[1]!);
      return jsonResponse({
        ...user,
        is_admin: (body as { role: string }).role === "administrator",
      });
    }
    const status = path.match(/^\/v1\/users\/([^/]+)\/status$/);
    if (status) {
      const { user } = rowFor(accounts, status[1]!);
      return jsonResponse({ ...user, status: (body as { status: ApiUser["status"] }).status });
    }
    const waiver = path.match(/^\/v1\/billing\/accounts\/([^/]+)\/complimentary$/);
    if (waiver) {
      const granted = (body as { complimentary: boolean }).complimentary;
      // An absent complimentary_since is how the API says the waiver is off.
      return jsonResponse({
        ...rowFor(accounts, waiver[1]!),
        complimentary_since: granted ? "2026-09-03T00:00:00Z" : undefined,
      });
    }
    return jsonResponse({ accounts });
  });
  return {
    puts: () =>
      sent
        .filter((call) => call.method === "PUT")
        .map((call) => ({ path: call.path, body: call.body })),
  };
}

function rowFor(accounts: ApiAccount[], userId: string): ApiAccount {
  const row = accounts.find((candidate) => candidate.user.id === userId);
  if (!row) throw new Error(`no fixture for ${userId}`);
  return row;
}

function account(
  id: string,
  overrides: Partial<ApiUser> = {},
  complimentarySince?: string,
): ApiAccount {
  return {
    user: {
      id,
      display_name: id,
      email: `${id}@example.com`,
      avatar_url: "",
      github_login: id,
      is_admin: false,
      status: "active",
      created_at: "2026-07-21T10:00:00Z",
      ...overrides,
    },
    status: "active",
    plan: "free",
    payment_method_on_file: false,
    complimentary_since: complimentarySince,
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

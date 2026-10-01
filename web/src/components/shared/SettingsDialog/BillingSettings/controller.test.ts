import { testQueryClient } from "@/test/query-client";
import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { useBillingSettingsController } from "./controller";

type Account = Schemas["BillingAccount"];

describe("billing settings controller", () => {
  it("sends a cardless account to the card page and starts no plan change", async () => {
    // The server refuses a cardless move onto a monthly plan before it writes
    // anything, so a change started here would spend the account's one
    // open-change slot on a request that was never going to work. Coming back
    // from the hosted card page must not be what authorises the charge either,
    // which is why the card is an action of its own rather than a step inside
    // the change.
    const requests = recordRequests(account({ payment_method_on_file: false }));
    const assign = stubNavigation();
    const { result } = await mountedController();

    const team = result.current.offers.find((offer) => offer.id === "team");
    expect(team?.action).toBe("card");
    act(() => result.current.choose(team!));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://provider.example/card"));

    expect(requests.planChanges()).toEqual([]);
    expect(requests.sent("POST", "/v1/billing/payment-method-sessions")).toHaveLength(1);
  });

  it("asks before charging a card, and sends nothing until it is answered", async () => {
    // The move up takes an immediate prorated charge inside the request that
    // makes it. A dialog click is not consent to that, so the send waits.
    const moved = account({ payment_method_on_file: true, plan: plan("team", "Team") });
    const requests = recordRequests(account({ payment_method_on_file: true }), moved);
    const { result } = await mountedController();

    const team = result.current.offers.find((offer) => offer.id === "team");
    expect(team?.action).toBe("switch");
    act(() => result.current.choose(team!));

    expect(result.current.confirmingChangeTo?.id).toBe("team");
    expect(requests.planChanges()).toEqual([]);

    act(() => result.current.confirmChange());
    await waitFor(() =>
      expect(requests.planChanges()).toEqual([{ plan: "team", terms_version: "team-v3" }]),
    );
  });

  it("confirms before moving down, then sends the plan that was confirmed", async () => {
    const moved = account({
      payment_method_on_file: true,
      plan: {
        ...plan("team", "Team"),
        scheduled_terms_version: "free-v2",
        scheduled_change_at: "2026-09-01T00:00:00Z",
      },
    });
    const requests = recordRequests(
      account({ payment_method_on_file: true, plan: plan("team", "Team") }),
      moved,
    );
    const { result } = await mountedController();

    const free = result.current.offers.find((offer) => offer.id === "free");
    expect(free?.action).toBe("downgrade");
    act(() => result.current.choose(free!));

    // Asking is not doing: nothing has been sent yet.
    expect(result.current.confirmingChangeTo?.id).toBe("free");
    expect(requests.planChanges()).toEqual([]);

    act(() => result.current.confirmChange());
    await waitFor(() =>
      expect(requests.planChanges()).toEqual([{ plan: "free", terms_version: "free-v2" }]),
    );
    await waitFor(() =>
      expect(result.current.summary?.plan.scheduled_terms_version).toBe("free-v2"),
    );
    expect(result.current.summary?.plan.id).toBe("team");

    // Choosing the held terms again is what cancels the scheduled move.
    act(() => result.current.cancelScheduledChange());
    await waitFor(() =>
      expect(requests.planChanges()).toEqual([
        { plan: "free", terms_version: "free-v2" },
        { plan: "team", terms_version: "team-v3" },
      ]),
    );
  });
});

async function mountedController(queryClient: QueryClient = testQueryClient()) {
  const rendered = renderHook(
    () => useBillingSettingsController({ planOpen: true, onPlanOpenChange: vi.fn() }),
    {
      wrapper: wrapper(queryClient),
    },
  );
  await waitFor(() => expect(rendered.result.current.summary).toBeDefined());
  return rendered;
}

function wrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

/** Every call the controller makes, with the bodies it sent. */
function recordRequests(initial: Account, afterChange: Account = initial) {
  const sent: { path: string; method: string; body: unknown }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const request = input as Request;
    const path = new URL(request.url).pathname;
    const text = await request.text();
    sent.push({ path, method: request.method, body: text ? JSON.parse(text) : undefined });
    if (path === "/v1/billing/payment-method-sessions") {
      return jsonResponse({ url: "https://provider.example/card" }, 201);
    }
    if (path === "/v1/pricing") return jsonResponse(pricingCatalog());
    if (path === "/v1/billing/plan") return jsonResponse(afterChange);
    return jsonResponse(initial);
  });
  return {
    sent: (method: string, path: string) =>
      sent.filter((call) => call.path === path && call.method === method).map((call) => call.body),
    planChanges: () =>
      sent
        .filter((call) => call.path === "/v1/billing/plan" && call.method === "PUT")
        .map((call) => call.body),
  };
}

/** jsdom refuses a real navigation, and leaving is what the card action does. */
function stubNavigation() {
  const assign = vi.fn();
  vi.spyOn(window, "location", "get").mockReturnValue({
    ...window.location,
    href: "http://dashboard.example/w/main/apps?settings=billing",
    assign,
  } as unknown as Location);
  return assign;
}

function plan(id: "free" | "team", name: string): Account["plan"] {
  return {
    id,
    name,
    terms_version: id === "free" ? "free-v2" : "team-v3",
    monthly_nanos: id === "free" ? 0 : 49_000_000_000,
    included_nanos: id === "free" ? 0 : 10_000_000_000,
    period_started_at: "2026-08-01T00:00:00Z",
    period_ended_at: "2026-09-01T00:00:00Z",
  };
}

function account(overrides: Partial<Account> = {}): Account {
  const month = {
    month_started_at: "2026-08-01T00:00:00Z",
    month_ended_at: "2026-09-01T00:00:00Z",
  };
  return {
    status: "active",
    currency: "USD",
    plan: plan("free", "Free"),
    portal_available: true,
    payment_method_on_file: false,
    entitlements: entitlements(),
    usage: {
      concurrent_cpu_containers: 0,
      concurrent_gpus: 0,
      workspaces: 1,
      members: 1,
      connected_clouds: 0,
      custom_domains: 0,
    },
    balance_nanos: 2_000_000_000,
    usage_budget: { ...month, spent_nanos: 0 },
    preferences: { reload_enabled: false, reload_threshold_cents: 500, reload_amount_cents: 500 },
    automatic_reload: { ...month, monthly_payment_committed_cents: 0 },
    plan_change_pending: false,
    ...overrides,
  };
}

function entitlements(): Schemas["PlanEntitlements"] {
  return {
    max_concurrent_cpu_containers: 30,
    max_concurrent_gpus: 5,
    gpu_types: ["T4", "L4", "A10G"],
    max_workspaces: 1,
    max_members: 1,
    connected_cloud: false,
    custom_domains: false,
    self_hosted: true,
    region_selection: false,
    retention_days: 1,
    max_workspace_disk_gib: 0,
  };
}

function pricingCatalog(): Schemas["PricingCatalog"] {
  return {
    pricing_version: "test",
    metered_rates_effective_at: "2026-09-11T00:00:00Z",
    currency: "USD",
    connected_cloud_management_fee_percent: 8,
    credit_purchase: { minimum_cents: 2000, maximum_cents: 100000 },
    trial: { amount_nanos: 2_000_000_000, duration_days: 30, one_time: true },
    no_payment_method: {
      max_concurrent_cpu_containers: 10,
      max_concurrent_gpus: 1,
      gpu_types: ["T4", "L4", "A10G"],
    },
    plans: [
      {
        id: "free",
        terms_version: "free-v2",
        name: "Free",
        summary: "Free plan",
        monthly_nanos: 0,
        included_nanos: 0,
        entitlements: entitlements(),
        terms: [],
      },
      {
        id: "team",
        terms_version: "team-v3",
        name: "Team",
        summary: "Team plan",
        monthly_nanos: 49_000_000_000,
        included_nanos: 10_000_000_000,
        entitlements: {
          ...entitlements(),
          max_concurrent_cpu_containers: 1_000,
          max_concurrent_gpus: 50,
          max_workspaces: undefined,
          max_members: 3,
          region_selection: true,
          retention_days: 30,
          custom_domains: true,
        },
        terms: [],
      },
    ],
    shape_rates: [],
    gpu_rates: [],
    placement_rates: [],
    platform_rate: {
      nanos_per_egress_gib: 0,
      nanos_per_volume_gib_month: 50_000_000,
      storage_month_seconds: 2_592_000,
    },
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

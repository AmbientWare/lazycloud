import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BillingSummary, PricingCatalog } from "@/lib/api/schemas";

import { useBillingSettingsController } from "./controller";

afterEach(() => vi.restoreAllMocks());

describe("billing settings controller", () => {
  it("sends a cardless account to the card page and starts no plan change", async () => {
    // The server refuses a cardless move onto a monthly plan before it writes
    // anything, so a change started here would spend the account's one
    // open-change slot on a request that was never going to work. Coming back
    // from the hosted card page must not be what authorises the charge either,
    // which is why the card is an action of its own rather than a step inside
    // the change.
    const requests = recordRequests(summary({ payment_method_on_file: false }));
    const assign = stubNavigation();
    const { result } = await mountedController();

    const team = result.current.offers.find((offer) => offer.id === "team");
    expect(team?.action).toBe("card");
    act(() => result.current.choose(team!));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://provider.example/card"));

    expect(requests.posts("/api/v1/billing/subscription")).toEqual([]);
    expect(requests.posts("/api/v1/billing/card-session")).toHaveLength(1);
  });

  it("asks before charging a card, and sends nothing until it is answered", async () => {
    // The move up takes an immediate prorated charge inside the request that
    // makes it. A dialog click is not consent to that, so the send waits.
    const moved = summary({ plan: plan("team", "Team") });
    const requests = recordRequests(summary({ payment_method_on_file: true }), moved);
    const { result } = await mountedController();

    const team = result.current.offers.find((offer) => offer.id === "team");
    expect(team?.action).toBe("switch");
    act(() => result.current.choose(team!));

    expect(result.current.confirmingChangeTo?.id).toBe("team");
    expect(requests.posts("/api/v1/billing/subscription")).toEqual([]);

    act(() => result.current.confirmChange());
    await waitFor(() =>
      expect(requests.posts("/api/v1/billing/subscription")).toEqual([
        { plan: "team", terms_version: "team-v2" },
      ]),
    );
  });

  it("confirms before moving down, then sends the plan that was confirmed", async () => {
    const moved = summary({
      plan: {
        ...plan("team", "Team"),
        scheduled_terms_version: "free-v2",
        scheduled_change_at: "2026-09-01T00:00:00Z",
      },
    });
    const requests = recordRequests(
      summary({ payment_method_on_file: true, plan: plan("team", "Team") }),
      moved,
    );
    const { result } = await mountedController();

    const free = result.current.offers.find((offer) => offer.id === "free");
    expect(free?.action).toBe("downgrade");
    act(() => result.current.choose(free!));

    // Asking is not doing: nothing has been sent yet.
    expect(result.current.confirmingChangeTo?.id).toBe("free");
    expect(requests.posts("/api/v1/billing/subscription")).toEqual([]);

    act(() => result.current.confirmChange());
    await waitFor(() =>
      expect(requests.posts("/api/v1/billing/subscription")).toEqual([
        { plan: "free", terms_version: "free-v2" },
      ]),
    );
    await waitFor(() =>
      expect(result.current.summary?.plan?.scheduled_terms_version).toBe("free-v2"),
    );
    expect(result.current.summary?.plan?.id).toBe("team");
  });

  it("compares verified legacy terms and can cancel a scheduled move to the new version", async () => {
    const legacy = {
      ...plan("team", "Team"),
      terms_version: "team-v1" as const,
      monthly_nanos: 100_000_000_000,
      included_nanos: 30_000_000_000,
      credit_scope: "all_metered" as const,
    };
    const requests = recordRequests(
      summary({ payment_method_on_file: true, plan: legacy }),
      summary({
        payment_method_on_file: true,
        plan: {
          ...legacy,
          scheduled_terms_version: "team-v2",
          scheduled_change_at: "2026-09-01T00:00:00Z",
        },
      }),
    );
    const { result } = await mountedController();
    const team = result.current.offers.find((offer) => offer.id === "team");
    expect(team?.action).toBe("downgrade");
    act(() => result.current.choose(team!));
    act(() => result.current.confirmChange());
    await waitFor(() =>
      expect(result.current.summary?.plan?.scheduled_terms_version).toBe("team-v2"),
    );
    expect(result.current.summary?.plan?.monthly_nanos).toBe(100_000_000_000);
    act(() => result.current.cancelScheduledChange());
    await waitFor(() =>
      expect(requests.posts("/api/v1/billing/subscription")).toEqual([
        { plan: "team", terms_version: "team-v2" },
        { plan: "team", terms_version: "team-v1" },
      ]),
    );
  });

  it("does not offer a plan change against unverified subscription terms", async () => {
    const requests = recordRequests(
      summary({
        payment_method_on_file: true,
        plan: {
          ...plan("team", "Team"),
          terms_version: null,
          monthly_nanos: null,
          included_nanos: null,
          credit_scope: null,
        },
      }),
    );
    const { result } = await mountedController();
    const free = result.current.offers.find((offer) => offer.id === "free");
    expect(free?.action).toBe("unverified");
    act(() => result.current.choose(free!));
    act(() => result.current.confirmChange());
    expect(result.current.confirmingChangeTo).toBeNull();
    expect(requests.posts("/api/v1/billing/subscription")).toEqual([]);
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

function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

/** Every call the controller makes, with the bodies it sent. */
function recordRequests(initial: BillingSummary, afterChange: BillingSummary = initial) {
  const sent: { path: string; method: string; body: unknown }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = new URL(String(input), "http://dashboard.example").pathname;
    const method = init?.method ?? "GET";
    sent.push({
      path,
      method,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    });
    if (path === "/api/v1/billing/card-session") {
      return jsonResponse({ url: "https://provider.example/card" }, 201);
    }
    if (path === "/api/v1/pricing") return jsonResponse(pricingCatalog());
    if (method === "POST") return jsonResponse(afterChange);
    return jsonResponse(initial);
  });
  return {
    posts: (path: string) =>
      sent.filter((call) => call.path === path && call.method === "POST").map((call) => call.body),
  };
}

/** jsdom refuses a real navigation, and leaving is what the card action does. */
function stubNavigation() {
  const assign = vi.fn();
  vi.spyOn(window, "location", "get").mockReturnValue({
    ...window.location,
    href: "http://dashboard.example/w/main/apps?settings=general",
    assign,
  } as unknown as Location);
  return assign;
}

function plan(id: "free" | "team", name: string): NonNullable<BillingSummary["plan"]> {
  return {
    id,
    name,
    terms_version: id === "free" ? "free-v2" : "team-v2",
    monthly_nanos: id === "free" ? 0 : 49_000_000_000,
    included_nanos: id === "free" ? 0 : 10_000_000_000,
    credit_scope: "compute",
    scheduled_terms_version: null,
    scheduled_change_at: null,
    period_started_at: "2026-08-01T00:00:00Z",
    period_ended_at: "2026-09-01T00:00:00Z",
  };
}

function summary(overrides: Partial<BillingSummary> = {}): BillingSummary {
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
    complimentary_since: null,
    plan_change_pending: false,
    ...overrides,
  };
}

function entitlements() {
  return {
    max_concurrent_cpu_containers: 30,
    max_concurrent_gpus: 5,
    gpu_types: ["T4", "L4", "A10G"],
    max_workspaces: 1 as const,
    max_members: 1 as const,
    connected_cloud: false,
    custom_domains: false,
    self_hosted: true,
    region_selection: false,
    log_retention_days: 1,
  };
}

function pricingCatalog(): PricingCatalog {
  return {
    pricing_version: "test",
    metered_rates_effective_at: "2026-09-11T00:00:00Z",
    currency: "USD",
    connected_cloud_management_fee_percent: 8,
    credit_purchase: { minimum_cents: 2000, maximum_cents: 100000 },
    trial: { amount_nanos: 5_000_000_000, duration_days: 30, scope: "all_metered", one_time: true },
    no_payment_method: {
      max_concurrent_cpu_containers: 10,
      max_concurrent_gpus: 1,
    },
    plans: [
      {
        id: "free",
        terms_version: "free-v2",
        credit_scope: "compute",
        name: "Free",
        summary: "Free plan",
        monthly_nanos: 0,
        included_nanos: 0,
        entitlements: entitlements(),
        terms: [],
      },
      {
        id: "team",
        terms_version: "team-v2",
        credit_scope: "compute",
        name: "Team",
        summary: "Team plan",
        monthly_nanos: 49_000_000_000,
        included_nanos: 10_000_000_000,
        entitlements: {
          ...entitlements(),
          max_concurrent_cpu_containers: 1_000,
          max_concurrent_gpus: 50,
          gpu_types: "all",
          max_workspaces: "unlimited",
          max_members: "unlimited",
          connected_cloud: true,
          region_selection: true,
          log_retention_days: 30,
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

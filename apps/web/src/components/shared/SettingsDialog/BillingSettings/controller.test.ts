import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BillingSummary } from "@/lib/api/schemas";

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
      expect(requests.posts("/api/v1/billing/subscription")).toEqual([{ plan: "team" }]),
    );
  });

  it("confirms before moving down, then sends the plan that was confirmed", async () => {
    const moved = summary({ plan: plan("free", "Free") });
    const requests = recordRequests(
      summary({ payment_method_on_file: true, plan: plan("team", "Team") }),
      moved,
    );
    const { result } = await mountedController();

    const free = result.current.offers.find((offer) => offer.id === "free");
    expect(free?.action).toBe("cancel");
    act(() => result.current.choose(free!));

    // Asking is not doing: nothing has been sent yet.
    expect(result.current.confirmingChangeTo?.id).toBe("free");
    expect(requests.posts("/api/v1/billing/subscription")).toEqual([]);

    act(() => result.current.confirmChange());
    await waitFor(() =>
      expect(requests.posts("/api/v1/billing/subscription")).toEqual([{ plan: "free" }]),
    );
  });
});

async function mountedController(queryClient: QueryClient = testQueryClient()) {
  const rendered = renderHook(() => useBillingSettingsController(), {
    wrapper: wrapper(queryClient),
  });
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

function plan(id: "free" | "team", name: string): BillingSummary["plan"] {
  return {
    id,
    name,
    allowance: {
      period_started_at: "2026-08-01T00:00:00Z",
      period_ended_at: "2026-09-01T00:00:00Z",
      allowance_nanos: 5_000_000_000,
      spent_nanos: 1_000_000_000,
      remaining_nanos: 4_000_000_000,
    },
  };
}

function summary(overrides: Partial<BillingSummary> = {}): BillingSummary {
  return {
    status: "active",
    currency: "USD",
    plan: plan("free", "Free"),
    portal_available: true,
    payment_method_on_file: false,
    max_concurrent_containers: 200,
    live_container_count: 0,
    plan_change_pending: false,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

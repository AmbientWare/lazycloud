import { testQueryClient } from "@/test/query-client";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, onTestFinished, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import {
  billingPreferencesSchema,
  type BillingPreferences,
  type PricingCatalog,
} from "@/lib/api/schemas";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { pricingCatalogQueryOptions } from "@/lib/queries/pricing";

import { BillingPreferences as BillingPreferencesForm } from "./BillingPreferences";
import { PrepaidCredit } from "./PrepaidCredit";

it("saves preset and custom amounts without changing untouched settings or confusing zero with no limit", async () => {
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: vi.fn(),
  });
  onTestFinished(() => {
    Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  const client = testQueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  let stored: BillingPreferences = {
    monthly_usage_limit_nanos: null,
    reload_enabled: false,
    reload_threshold_cents: 500,
    reload_amount_cents: 500,
  };
  client.setQueryData<PricingCatalog>(pricingCatalogQueryOptions().queryKey, {
    pricing_version: "test",
    metered_rates_effective_at: "2026-09-01T00:00:00Z",
    currency: "USD",
    connected_cloud_management_fee_percent: 8,
    credit_purchase: { minimum_cents: 500, maximum_cents: 100000 },
    trial: { amount_nanos: 2_000_000_000, duration_days: 30, one_time: true },
    no_payment_method: {
      max_concurrent_cpu_containers: 10,
      max_concurrent_gpus: 1,
      gpu_types: ["T4", "L4", "A10G"],
    },
    plans: [],
    shape_rates: [],
    gpu_rates: [],
    placement_rates: [],
    platform_rate: {
      nanos_per_egress_gib: 0,
      nanos_per_volume_gib_month: 0,
      storage_month_seconds: 2592000,
    },
    disk_rate: null,
  });
  let resolveSave: ((response: Response) => void) | undefined;
  const response = new Promise<Response>((resolve) => {
    resolveSave = resolve;
  });
  let holdSave = true;
  vi.stubGlobal("fetch", async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (path === "/v1/billing/preferences" && request.method === "PUT") {
      const body = (await request.json()) as Schemas["BillingPreferences"];
      stored = billingPreferencesSchema.parse({
        ...body,
        monthly_usage_limit_nanos: body.monthly_usage_limit_nanos ?? null,
      });
      return holdSave ? response : Response.json(billingAccount(stored));
    }
    if (path === "/v1/billing" && request.method === "GET") {
      return Response.json(billingAccount(stored));
    }
    throw new Error(`Unexpected request: ${request.method} ${path}`);
  });
  await client.fetchQuery(billingSummaryQueryOptions());
  const { rerender } = render(
    <QueryClientProvider client={client}>
      <PrepaidCredit paymentMethodOnFile />
      <BillingPreferencesForm paymentMethodOnFile />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Add credits" })).toBeEnabled();
  fireEvent.keyDown(screen.getByLabelText("Monthly usage limit, USD"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("option", { name: "$50.00" }));
  fireEvent.click(screen.getByLabelText("Automatic reload"));
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(screen.getByLabelText("Monthly usage limit, USD")).toBeDisabled());
  holdSave = false;
  await act(async () => resolveSave?.(Response.json(billingAccount(stored))));
  await waitFor(() => expect(screen.getByLabelText("Monthly usage limit, USD")).toBeEnabled());
  await screen.findByText("Changes saved.");
  expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  expect(stored.reload_amount_cents).toBe(500);
  fireEvent.keyDown(screen.getByLabelText("Add, USD"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("option", { name: "Custom amount" }));
  fireEvent.change(screen.getByLabelText("Add, USD, custom amount"), {
    target: { value: "32.75" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await screen.findByText("Changes saved.");
  expect(stored.reload_enabled).toBe(true);
  expect(stored.monthly_usage_limit_nanos).toBe(50_000_000_000);
  expect(stored.reload_amount_cents).toBe(3275);

  rerender(
    <QueryClientProvider client={client}>
      <PrepaidCredit paymentMethodOnFile={false} />
      <BillingPreferencesForm paymentMethodOnFile={false} />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Add credits" })).toBeDisabled();
  expect(screen.getByLabelText("Amount in USD")).toBeDisabled();
  expect(screen.getByLabelText("Add, USD")).toBeDisabled();
  expect(screen.getByLabelText("Add, USD, custom amount")).toBeDisabled();
  fireEvent.click(screen.getByLabelText("Automatic reload"));
  expect(screen.getByLabelText("Automatic reload")).toBeDisabled();
  fireEvent.keyDown(screen.getByLabelText("Monthly usage limit, USD"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("option", { name: "$0.00" }));
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await screen.findByText("Changes saved.");
  expect(stored.monthly_usage_limit_nanos).toBe(0);
  expect(stored.reload_enabled).toBe(false);

  fireEvent.keyDown(screen.getByLabelText("Monthly usage limit, USD"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("option", { name: "No limit" }));
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await screen.findByText("Changes saved.");
  expect(stored.monthly_usage_limit_nanos).toBeNull();
});

/** The account `GET /v1/billing` answers, holding the saved preferences. */
function billingAccount(preferences: BillingPreferences): Schemas["BillingAccount"] {
  const month = {
    month_started_at: "2026-09-01T00:00:00Z",
    month_ended_at: "2026-10-01T00:00:00Z",
  };
  return {
    status: "active",
    currency: "USD",
    plan: {
      id: "free",
      name: "Free",
      terms_version: "free-v2",
      monthly_nanos: 0,
      included_nanos: 0,
    },
    portal_available: true,
    payment_method_on_file: true,
    entitlements: {
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
    },
    usage: {
      concurrent_cpu_containers: 0,
      concurrent_gpus: 0,
      workspaces: 1,
      members: 1,
      connected_clouds: 0,
      custom_domains: 0,
    },
    balance_nanos: 5_000_000_000,
    usage_budget: { ...month, spent_nanos: 0 },
    preferences: {
      reload_enabled: preferences.reload_enabled,
      reload_threshold_cents: preferences.reload_threshold_cents,
      reload_amount_cents: preferences.reload_amount_cents,
      monthly_usage_limit_nanos: preferences.monthly_usage_limit_nanos ?? undefined,
    },
    automatic_reload: { ...month, monthly_payment_committed_cents: 0 },
    plan_change_pending: false,
  };
}

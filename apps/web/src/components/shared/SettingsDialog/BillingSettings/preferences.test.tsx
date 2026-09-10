import { testQueryClient } from "@/test/query-client";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, onTestFinished, vi } from "vitest";

import {
  billingPreferencesSchema,
  type BillingPreferences,
  type PricingCatalog,
} from "@/lib/api/schemas";
import {
  automaticReloadStatusQueryOptions,
  billingPreferencesQueryOptions,
  creditBalanceQueryOptions,
  usageBudgetQueryOptions,
} from "@/lib/queries/billing";
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
  client.setQueryData(billingPreferencesQueryOptions().queryKey, stored);
  client.setQueryData(creditBalanceQueryOptions().queryKey, {
    ready: true,
    balance_nanos: 5_000_000_000,
  });
  client.setQueryData<PricingCatalog>(pricingCatalogQueryOptions().queryKey, {
    pricing_version: "test",
    metered_rates_effective_at: "2026-09-01T00:00:00Z",
    currency: "USD",
    connected_cloud_management_fee_percent: 8,
    credit_purchase: { minimum_cents: 500, maximum_cents: 100000 },
    trial: { amount_nanos: 5_000_000_000, duration_days: 30, one_time: true },
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
  });
  const month = {
    month_started_at: "2026-09-01T00:00:00Z",
    month_ended_at: "2026-10-01T00:00:00Z",
  };
  const status = {
    ...month,
    paused_purchase_id: null,
    pause_reason: null,
    pending_purchase_id: null,
    monthly_payment_committed_cents: 0,
  };
  const budget = { ...month, limit_nanos: null, spent_nanos: 0, available_nanos: null };
  client.setQueryData(automaticReloadStatusQueryOptions().queryKey, status);
  client.setQueryData(usageBudgetQueryOptions().queryKey, budget);
  let resolveSave: ((response: Response) => void) | undefined;
  const response = new Promise<Response>((resolve) => {
    resolveSave = resolve;
  });
  let holdSave = true;
  vi.stubGlobal("fetch", async (path: string, init?: RequestInit) => {
    if (path.endsWith("/preferences") && init?.method === "PUT") {
      stored = billingPreferencesSchema.parse(JSON.parse(String(init.body)));
      return holdSave ? response : Response.json(stored);
    }
    if (path.endsWith("/usage-budget")) return Response.json(budget);
    if (path.endsWith("/automatic-reload")) return Response.json(status);
    throw new Error(`Unexpected request: ${path}`);
  });
  const { rerender } = render(
    <QueryClientProvider client={client}>
      <PrepaidCredit paymentMethodOnFile />
      <BillingPreferencesForm paymentMethodOnFile />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Add credit" })).toBeEnabled();
  fireEvent.keyDown(screen.getByLabelText("Monthly usage limit, USD"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("option", { name: "$50.00" }));
  fireEvent.click(screen.getByLabelText("Automatic reload"));
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(screen.getByLabelText("Monthly usage limit, USD")).toBeDisabled());
  holdSave = false;
  await act(async () => resolveSave?.(Response.json(stored)));
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
  expect(screen.getByRole("button", { name: "Add credit" })).toBeDisabled();
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

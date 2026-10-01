import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { accountQueryKeys } from "./workspace-keys";

// Provider callbacks can arrive after the hosted page redirects back, and usage
// moves the balance; the change stream carries neither.
const BILLING_POLL_INTERVAL_MS = 5_000;

function settling(account: Schemas["BillingAccount"] | undefined): boolean {
  return Boolean(
    account &&
    (!account.payment_method_on_file ||
      account.plan_change_pending ||
      account.plan.scheduled_change_at),
  );
}

/**
 * `GET /v1/billing` answers the plan, balance, spending controls and reload
 * state together, so one cache entry holds the account. Every billing command
 * answers the same account, which replaces it.
 *
 * Views that show the balance pass `balance` and poll it; the rest poll only
 * while a payment or plan change is settling.
 */
export function billingAccountQueryOptions({ balance = false }: { balance?: boolean } = {}) {
  return queryOptions({
    queryKey: accountQueryKeys.billing(),
    queryFn: () => ok(api.GET("/v1/billing")),
    staleTime: 30_000,
    refetchInterval: (query) =>
      balance || settling(query.state.data) ? BILLING_POLL_INTERVAL_MS : false,
  });
}

export function saveBillingPreferences(preferences: Schemas["BillingPreferences"]) {
  return ok(api.PUT("/v1/billing/preferences", { body: preferences }));
}

export function resumeAutomaticReload() {
  return ok(api.POST("/v1/billing/automatic-reload/resume"));
}

export function changeBillingPlan(selection: Schemas["PlanChangeRequest"]) {
  return ok(api.PUT("/v1/billing/plan", { body: selection }));
}

function hostedSessionRequest() {
  const returnUrl = window.location.href;
  return { return_url: returnUrl, cancel_url: returnUrl };
}

export async function startCardSetup(): Promise<void> {
  const session = await ok(
    api.POST("/v1/billing/payment-method-sessions", { body: hostedSessionRequest() }),
  );
  window.location.assign(session.url);
}

export async function openBillingPortal(): Promise<void> {
  const session = await ok(
    api.POST("/v1/billing/portal-sessions", { body: hostedSessionRequest() }),
  );
  window.location.assign(session.url);
}

export async function purchaseCredit(request: { requestKey: string; amountCents: number }) {
  const purchase = await ok(
    api.POST("/v1/billing/credit-purchases", {
      body: {
        request_key: request.requestKey,
        amount_cents: request.amountCents,
        ...hostedSessionRequest(),
      },
    }),
  );
  if (purchase.checkout_url) window.location.assign(purchase.checkout_url);
  return purchase;
}

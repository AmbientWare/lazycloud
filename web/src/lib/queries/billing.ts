import { queryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import {
  billingHostedSessionResponseSchema,
  billingSummarySchema,
  creditPurchaseSchema,
  creditBalanceSchema,
  billingPreferencesSchema,
  usageBudgetSchema,
  automaticReloadStatusSchema,
  type BillingPreferences,
  type BillingPlanId,
  type BillingTermsVersion,
  type BillingSummary,
} from "@/lib/api/schemas";

import { accountQueryKeys } from "./workspace-keys";

// Provider callbacks can arrive after the hosted page redirects back.
const BILLING_SUMMARY_POLL_INTERVAL_MS = 5_000;

export function billingSummaryQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.billing(),
    queryFn: () => apiRequest("/api/v1/billing/summary", billingSummarySchema),
    staleTime: 30_000,
    refetchInterval: (query) =>
      query.state.data?.payment_method_on_file === false ||
      query.state.data?.plan_change_pending ||
      query.state.data?.plan?.terms_version === null ||
      Boolean(query.state.data?.plan?.scheduled_change_at)
        ? BILLING_SUMMARY_POLL_INTERVAL_MS
        : false,
  });
}

export function creditBalanceQueryOptions() {
  return queryOptions({
    queryKey: [...accountQueryKeys.billing(), "credits"],
    queryFn: () => apiRequest("/api/v1/billing/credits", creditBalanceSchema),
    refetchInterval: 5_000,
  });
}

export function billingPreferencesQueryOptions() {
  return queryOptions({
    queryKey: [...accountQueryKeys.billing(), "preferences"],
    queryFn: () => apiRequest("/api/v1/billing/preferences", billingPreferencesSchema),
  });
}

export function usageBudgetQueryOptions() {
  return queryOptions({
    queryKey: [...accountQueryKeys.billing(), "usage-budget"],
    queryFn: () => apiRequest("/api/v1/billing/usage-budget", usageBudgetSchema),
    refetchInterval: 5_000,
  });
}

export function saveBillingPreferences(preferences: BillingPreferences) {
  return apiRequest("/api/v1/billing/preferences", billingPreferencesSchema, {
    method: "PUT",
    body: JSON.stringify(preferences),
  });
}

export function automaticReloadStatusQueryOptions() {
  return queryOptions({
    queryKey: [...accountQueryKeys.billing(), "automatic-reload"],
    queryFn: () => apiRequest("/api/v1/billing/automatic-reload", automaticReloadStatusSchema),
    refetchInterval: 5_000,
  });
}

export function resumeAutomaticReload() {
  return apiRequest("/api/v1/billing/automatic-reload/resume", automaticReloadStatusSchema, {
    method: "POST",
  });
}

async function openHostedSession(path: string): Promise<void> {
  const returnUrl = window.location.href;
  const session = await apiRequest(path, billingHostedSessionResponseSchema, {
    method: "POST",
    body: JSON.stringify({ return_url: returnUrl, cancel_url: returnUrl }),
  });
  window.location.assign(session.url);
}

export function startCardSetup(): Promise<void> {
  return openHostedSession("/api/v1/billing/card-session");
}

export function openBillingPortal(): Promise<void> {
  return openHostedSession("/api/v1/billing/portal-session");
}

export async function purchaseCredit(request: { requestKey: string; amountCents: number }) {
  const returnUrl = window.location.href;
  const purchase = await apiRequest("/api/v1/billing/credit-purchases", creditPurchaseSchema, {
    method: "POST",
    body: JSON.stringify({
      request_key: request.requestKey,
      amount_cents: request.amountCents,
      return_url: returnUrl,
      cancel_url: returnUrl,
    }),
  });
  if (purchase.checkout_url) window.location.assign(purchase.checkout_url);
  return purchase;
}

export function changeBillingPlan(selection: {
  plan: BillingPlanId;
  terms_version: BillingTermsVersion;
}): Promise<BillingSummary> {
  return apiRequest("/api/v1/billing/subscription", billingSummarySchema, {
    method: "POST",
    body: JSON.stringify(selection),
  });
}

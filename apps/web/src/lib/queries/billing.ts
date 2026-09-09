import { queryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import {
  billingHostedSessionResponseSchema,
  billingSummarySchema,
  creditPurchaseSchema,
  creditSummarySchema,
  billingPreferencesSchema,
  usageBudgetSchema,
  type BillingPreferences,
  type BillingPlanId,
  type BillingSummary,
} from "@/lib/api/schemas";

import { accountQueryKeys } from "./workspace-keys";

/**
 * What the signed-in account is on, and what it has left to spend.
 *
 * Keyed off the account rather than the workspace: the provider invoices a
 * person, and somebody holding three workspaces holds one payment relationship,
 * so switching workspace cannot change the answer.
 */
const CARDLESS_SUMMARY_POLL_INTERVAL_MS = 5_000;
/**
 * How often the summary is re-read while the account still has no card.
 *
 * Saving a card happens at the provider and reaches this platform as a delivery,
 * several hops after the customer has already been sent back here. Without a
 * poll the page they land on says they have no card — the state they just left
 * to fix — until something else happens to refetch. Polling only while the
 * answer can still change is the same shape the connected-cloud account uses
 * while it waits for a stack to finish.
 */

export function billingSummaryQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.billing(),
    queryFn: () => apiRequest("/api/v1/billing/summary", billingSummarySchema),
    staleTime: 30_000,
    refetchInterval: (query) =>
      query.state.data?.payment_method_on_file === false
        ? CARDLESS_SUMMARY_POLL_INTERVAL_MS
        : false,
  });
}

export function creditBalanceQueryOptions() {
  return queryOptions({
    queryKey: [...accountQueryKeys.billing(), "credits"],
    queryFn: () => apiRequest("/api/v1/billing/credits", creditSummarySchema),
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

/**
 * Ask the control plane for a page at the payment provider, and go there.
 *
 * Two of them, differing only in which page: one for somebody who has no card
 * saved and one for somebody managing the card and invoices they already have.
 * Neither renders anything here — the card is collected by the provider, which
 * is what keeps this app out of the way of card data entirely.
 *
 * `window.location.assign` rather than a router navigation, deliberately: the
 * destination is not this app, and the router would treat it as a route it does
 * not have.
 */
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

/**
 * Move this account onto a published plan, in either direction.
 *
 * The body names a plan id and never a price: the browser reads the catalog the
 * server derives from its rate card, so quoting a figure back would create a
 * second source of truth. Moving down is this same call — the
 * subscription is never cancelled, because ending it would take the metered
 * prices with it and leave this account's usage reaching no invoice.
 *
 * What comes back is the account's standing afterwards, so the section that
 * asked can show the new plan without a second round trip.
 *
 * Stays here rather than becoming a mutation option builder: it is one call with
 * no cache key of its own, and the caller writes the answer into the summary it
 * already reads.
 */
export function changeBillingPlan(plan: BillingPlanId): Promise<BillingSummary> {
  return apiRequest("/api/v1/billing/subscription", billingSummarySchema, {
    method: "POST",
    body: JSON.stringify({ plan }),
  });
}

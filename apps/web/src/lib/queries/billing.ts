import { queryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import {
  billingHostedSessionResponseSchema,
  billingSummarySchema,
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
export function billingSummaryQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.billing(),
    queryFn: () => apiRequest("/api/v1/billing/summary", billingSummarySchema),
    staleTime: 30_000,
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

/**
 * Move this account onto the Team plan.
 *
 * Bodiless, because there is one plan to move to — naming it from the browser
 * would be the browser holding a catalog it does not own. What comes back is the
 * account's standing afterwards, so the panel that asked can show the re-termed
 * period without a second round trip.
 *
 * Stays here rather than becoming a mutation option builder: it is one call with
 * no arguments and no cache key of its own, and the caller invalidates the
 * summary it already reads.
 */
export function subscribeToPlan(): Promise<BillingSummary> {
  return apiRequest("/api/v1/billing/subscription", billingSummarySchema, { method: "POST" });
}

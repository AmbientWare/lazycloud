import { apiRequest } from "@/lib/api/client";
import { billingHostedSessionResponseSchema } from "@/lib/api/schemas";

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

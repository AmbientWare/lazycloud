import { z } from "zod";

/**
 * The page to send the customer to.
 *
 * A URL and nothing else. Cards are collected, stored, and shown by the payment
 * provider and never reach this platform, so there is nothing else here to
 * render — the flow is a redirect, not a form.
 */
export const billingHostedSessionResponseSchema = z.object({
  url: z.string().min(1),
});
export type BillingHostedSessionResponse = z.infer<
  typeof billingHostedSessionResponseSchema
>;

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
export type BillingHostedSessionResponse = z.infer<typeof billingHostedSessionResponseSchema>;

export const billingAccountStatuses = ["active", "past_due"] as const;
export type BillingAccountStatus = (typeof billingAccountStatuses)[number];

export const billingPlanIds = ["free", "team"] as const;
export type BillingPlanId = (typeof billingPlanIds)[number];

/**
 * What this account may spend before the period costs it anything.
 *
 * `allowance_nanos` is what was stamped on the period when it opened, so a
 * change to what the platform grants does not restate the terms of a period
 * somebody is part-way through. `remaining_nanos` is signed and goes negative
 * once the allowance is overspent — clamping it would hide how far past the line
 * an account actually is, which is the one figure worth reading once it is.
 */
export const billingAllowanceSchema = z.object({
  period_started_at: z.string(),
  period_ended_at: z.string(),
  allowance_nanos: z.number().int().nonnegative(),
  spent_nanos: z.number().int().nonnegative(),
  remaining_nanos: z.number().int(),
});
export type BillingAllowance = z.infer<typeof billingAllowanceSchema>;

/**
 * The plan an account holds, and what its current cycle came with.
 *
 * `allowance` is null between a cycle ending and the renewal that opens the
 * next, which is a few minutes once a cycle — distinct from an allowance that is
 * spent, and from being on no plan at all.
 */
export const billingPlanSchema = z.object({
  id: z.enum(billingPlanIds),
  allowance: billingAllowanceSchema.nullable(),
});
export type BillingPlan = z.infer<typeof billingPlanSchema>;

export const billingSummarySchema = z.object({
  status: z.enum(billingAccountStatuses),
  currency: z.string().regex(/^[A-Z]{3}$/),
  // Null for an account on no subscription. Signing in puts an account on the
  // free plan, so anyone who did has one; what is left is an account reaching
  // the dashboard on a token an administrator minted for it, and one whose
  // subscription the provider says has ended. Neither has terms to be shown, and
  // both need the same thing next, which is a plan.
  plan: billingPlanSchema.nullable(),
  // Whether there is anything at the provider to manage. Signing in registers
  // the customer, so this is true for anyone who did; an account reaching the
  // dashboard on a token an administrator minted for it has no customer record,
  // and the management page would have nobody to show.
  portal_available: z.boolean(),
});
export type BillingSummary = z.infer<typeof billingSummarySchema>;

import { z } from "zod";

import { billingPlanIdSchema, planEntitlementsSchema } from "./pricing";
import { userSchema } from "./users";

const timestampSchema = z.string().datetime({ offset: true });

/**
 * The page to send the customer to.
 *
 * A URL and nothing else. Cards are collected, stored, and shown by the payment
 * provider and never reach this platform, so there is nothing else here to
 * render — the flow is a redirect, not a form.
 */
export const billingHostedSessionResponseSchema = z
  .object({
    url: z.string().min(1),
  })
  .strict();
export type BillingHostedSessionResponse = z.infer<typeof billingHostedSessionResponseSchema>;

export const billingAccountStatuses = ["active", "past_due"] as const;
export type BillingAccountStatus = (typeof billingAccountStatuses)[number];

/**
 * What this account may spend before the period costs it anything.
 *
 * `allowance_nanos` is what was stamped on the period when it opened, so a
 * change to what the platform grants does not restate the terms of a period
 * somebody is part-way through. `remaining_nanos` is signed and goes negative
 * once the allowance is overspent — clamping it would hide how far past the line
 * an account actually is, which is the one figure worth reading once it is.
 *
 * All three describe usage against this period's terms. None of them is the
 * amount the payment provider will collect, which includes things this platform
 * does not model — a balance carried from a period that fell under the
 * provider's minimum charge, a proration, tax — so nothing rendered from these
 * should be worded as what the invoice will say.
 */
export const billingAllowanceSchema = z
  .object({
    period_started_at: z.string(),
    period_ended_at: z.string(),
    allowance_nanos: z.number().int().nonnegative(),
    spent_nanos: z.number().int().nonnegative(),
    remaining_nanos: z.number().int(),
  })
  .strict();
export type BillingAllowance = z.infer<typeof billingAllowanceSchema>;

/**
 * The plan an account holds, and what its current cycle came with.
 *
 * `allowance` is null between a cycle ending and the renewal that opens the
 * next, which is a few minutes once a cycle — distinct from an allowance that is
 * spent, and from being on no plan at all.
 */
export const billingPlanSchema = z
  .object({
    id: billingPlanIdSchema,
    // Sent by the server from the same rate card as the pricing endpoint, so the
    // dashboard needs no local map for a plan name.
    name: z.string(),
    allowance: billingAllowanceSchema.nullable(),
  })
  .strict();
export type BillingPlan = z.infer<typeof billingPlanSchema>;

export const billingEntitlementUsageSchema = z
  .object({
    concurrent_cpu_containers: z.number().int().nonnegative(),
    concurrent_gpus: z.number().int().nonnegative(),
    workspaces: z.number().int().nonnegative(),
    members: z.number().int().nonnegative(),
    connected_clouds: z.number().int().nonnegative(),
    custom_domains: z.number().int().nonnegative(),
  })
  .strict();

export const billingSummarySchema = z
  .object({
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
    // A different question from the one above, which is true from the moment a
    // customer record exists and so cannot tell an account that has saved a card
    // from one that has not. How much the account is given, and whether its
    // running work is stopped when that is spent, both turn on this one.
    payment_method_on_file: z.boolean(),
    entitlements: planEntitlementsSchema.nullable(),
    usage: billingEntitlementUsageSchema,
    // When an administrator waived this account's bill, null while nobody has.
    // Usage is still priced and shown; none of it is owed. `plan` is what the
    // account returns to when the waiver is withdrawn, and `entitlements` are
    // the waiver's rather than the plan's.
    complimentary_since: timestampSchema.nullable(),
    // Whether a change of plan is still waiting on an outcome. `plan` above says
    // what the account holds, which is not what somebody who has just pressed a
    // button is asking; a change nobody could settle is retried for hours, and
    // without this the surface offers a button that answers 409.
    plan_change_pending: z.boolean(),
  })
  .strict();
export type BillingSummary = z.infer<typeof billingSummarySchema>;

/**
 * One account as an administrator sees it: the person, and what they owe.
 *
 * `status` and `plan` are null together for an account billing has never
 * written a row for, which is one that has not signed in yet. Such an account
 * can still be waived ahead of time, so `complimentary_since` may be set on a
 * row with no plan.
 */
export const billingAccountAdminSchema = z
  .object({
    user: userSchema,
    status: z.enum(billingAccountStatuses).nullable(),
    plan: billingPlanIdSchema.nullable(),
    payment_method_on_file: z.boolean(),
    complimentary_since: timestampSchema.nullable(),
    // What this account's usage cost over the trailing window, waived or not.
    recent_cost_nanos: z.number().int().nonnegative(),
    // Where that window starts; it ends at the moment of the request.
    recent_cost_since: timestampSchema,
  })
  .strict();
export type BillingAccountAdmin = z.infer<typeof billingAccountAdminSchema>;

export const billingAccountAdminListSchema = z
  .object({
    data: z.array(billingAccountAdminSchema),
    next: z.string(),
  })
  .strict();
export type BillingAccountAdminList = z.infer<typeof billingAccountAdminListSchema>;

export const billingComplimentaryRequestSchema = z
  .object({
    complimentary: z.boolean(),
  })
  .strict();
export type BillingComplimentaryRequest = z.infer<typeof billingComplimentaryRequestSchema>;

export type { BillingPlanId } from "./pricing";

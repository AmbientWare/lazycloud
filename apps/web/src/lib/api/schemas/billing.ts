import { z } from "zod";

import { billingPlanIdSchema, billingTermsVersionSchema, planEntitlementsSchema } from "./pricing";
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

export const creditPurchaseSchema = z
  .object({
    id: z.string().uuid(),
    amount_nanos: z.number().int().positive(),
    status: z.enum(["pending", "action_required", "succeeded", "declined", "cancelled"]),
    checkout_url: z.string().nullable(),
    funded_at: timestampSchema.nullable(),
    reversed_nanos: z.number().int().nonnegative(),
  })
  .strict();
export type CreditPurchase = z.infer<typeof creditPurchaseSchema>;

export const creditBalanceSchema = z
  .object({
    ready: z.boolean(),
    balance_nanos: z.number().int(),
  })
  .strict();
export type CreditBalance = z.infer<typeof creditBalanceSchema>;

export const billingPreferencesSchema = z
  .object({
    monthly_usage_limit_nanos: z
      .number()
      .int()
      .nonnegative()
      .max(Number.MAX_SAFE_INTEGER)
      .nullable(),
    reload_enabled: z.boolean(),
    reload_threshold_cents: z.number().int().nonnegative(),
    reload_amount_cents: z.number().int().positive(),
    reload_monthly_payment_limit_cents: z.number().int().nonnegative().nullable(),
  })
  .strict();
export type BillingPreferences = z.infer<typeof billingPreferencesSchema>;

export const automaticReloadStatusSchema = z
  .object({
    paused_purchase_id: z.string().uuid().nullable(),
    pause_reason: z.enum(["declined", "action_required"]).nullable(),
    pending_purchase_id: z.string().uuid().nullable(),
    month_started_at: timestampSchema,
    month_ended_at: timestampSchema,
    monthly_payment_committed_cents: z.number().int().nonnegative(),
  })
  .strict();

export const usageBudgetSchema = z
  .object({
    month_started_at: timestampSchema,
    month_ended_at: timestampSchema,
    limit_nanos: z.number().int().nonnegative().nullable(),
    spent_nanos: z.number().int().nonnegative(),
    available_nanos: z.number().int().nonnegative().nullable(),
  })
  .strict();

export const billingAccountStatuses = ["active", "past_due"] as const;
export type BillingAccountStatus = (typeof billingAccountStatuses)[number];

/** Verified subscription terms and cycle dates, separate from spendable credit. */
export const billingPlanSchema = z
  .object({
    id: billingPlanIdSchema,
    terms_version: billingTermsVersionSchema.nullable(),
    monthly_nanos: z.number().int().nonnegative().nullable(),
    included_nanos: z.number().int().nonnegative().nullable(),
    scheduled_terms_version: billingTermsVersionSchema.nullable(),
    scheduled_change_at: timestampSchema.nullable(),
    // Sent by the server from the same rate card as the pricing endpoint, so the
    // dashboard needs no local map for a plan name.
    name: z.string(),
    period_started_at: timestampSchema.nullable(),
    period_ended_at: timestampSchema.nullable(),
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

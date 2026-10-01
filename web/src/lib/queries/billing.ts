import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type {
  AutomaticReloadStatus,
  BillingPlanId,
  BillingPreferences,
  BillingSummary,
  BillingTermsVersion,
  CreditBalance,
  UsageBudget,
} from "@/lib/api/schemas";

import { toPlanEntitlements } from "./pricing";
import { accountQueryKeys } from "./workspace-keys";

/**
 * `GET /v1/billing` answers the plan, balance, spending controls and reload
 * state together, so one cache entry holds the account and each panel selects
 * its part. Every billing command answers the same account, which replaces it.
 */
export type BillingAccountState = BillingSummary & {
  balance_nanos: number;
  preferences: BillingPreferences;
  usage_budget: UsageBudget;
  automatic_reload: AutomaticReloadStatus;
};

// Provider callbacks can arrive after the hosted page redirects back.
const BILLING_SUMMARY_POLL_INTERVAL_MS = 5_000;

function toBillingAccountState(account: Schemas["BillingAccount"]): BillingAccountState {
  const { plan, usage_budget: budget, automatic_reload: reload } = account;
  return {
    status: account.status,
    currency: account.currency,
    plan: {
      id: plan.id,
      name: plan.name,
      terms_version: plan.terms_version,
      monthly_nanos: plan.monthly_nanos,
      included_nanos: plan.included_nanos,
      scheduled_terms_version: plan.scheduled_terms_version ?? null,
      scheduled_change_at: plan.scheduled_change_at ?? null,
      period_started_at: plan.period_started_at ?? null,
      period_ended_at: plan.period_ended_at ?? null,
    },
    portal_available: account.portal_available,
    payment_method_on_file: account.payment_method_on_file,
    entitlements: toPlanEntitlements(account.entitlements),
    usage: account.usage,
    complimentary_since: account.complimentary_since ?? null,
    plan_change_pending: account.plan_change_pending,
    balance_nanos: account.balance_nanos,
    preferences: {
      ...account.preferences,
      monthly_usage_limit_nanos: account.preferences.monthly_usage_limit_nanos ?? null,
    },
    usage_budget: {
      month_started_at: budget.month_started_at,
      month_ended_at: budget.month_ended_at,
      limit_nanos: budget.limit_nanos ?? null,
      spent_nanos: budget.spent_nanos,
      available_nanos: budget.available_nanos ?? null,
    },
    automatic_reload: {
      paused_purchase_id: reload.paused_purchase_id ?? null,
      pause_reason: reload.pause_reason ?? null,
      pending_purchase_id: reload.pending_purchase_id ?? null,
      month_started_at: reload.month_started_at,
      month_ended_at: reload.month_ended_at,
      monthly_payment_committed_cents: reload.monthly_payment_committed_cents,
    },
  };
}

async function fetchBillingAccount(): Promise<BillingAccountState> {
  return toBillingAccountState(await ok(api.GET("/v1/billing")));
}

export function billingSummaryQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.billing(),
    queryFn: fetchBillingAccount,
    staleTime: 30_000,
    refetchInterval: (query) =>
      query.state.data?.payment_method_on_file === false ||
      query.state.data?.plan_change_pending ||
      Boolean(query.state.data?.plan?.scheduled_change_at)
        ? BILLING_SUMMARY_POLL_INTERVAL_MS
        : false,
  });
}

export function creditBalanceQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.billing(),
    queryFn: fetchBillingAccount,
    select: (account): CreditBalance => ({ ready: true, balance_nanos: account.balance_nanos }),
    refetchInterval: 5_000,
  });
}

export function billingPreferencesQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.billing(),
    queryFn: fetchBillingAccount,
    select: (account) => account.preferences,
  });
}

export function usageBudgetQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.billing(),
    queryFn: fetchBillingAccount,
    select: (account) => account.usage_budget,
    refetchInterval: 5_000,
  });
}

export async function saveBillingPreferences(
  preferences: BillingPreferences,
): Promise<BillingAccountState> {
  const { monthly_usage_limit_nanos: limit, ...reload } = preferences;
  return toBillingAccountState(
    await ok(
      api.PUT("/v1/billing/preferences", {
        body: { ...reload, monthly_usage_limit_nanos: limit ?? undefined },
      }),
    ),
  );
}

export function automaticReloadStatusQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.billing(),
    queryFn: fetchBillingAccount,
    select: (account) => account.automatic_reload,
    refetchInterval: 5_000,
  });
}

export async function resumeAutomaticReload(): Promise<BillingAccountState> {
  return toBillingAccountState(await ok(api.POST("/v1/billing/automatic-reload/resume")));
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

export async function changeBillingPlan(selection: {
  plan: BillingPlanId;
  terms_version: BillingTermsVersion;
}): Promise<BillingAccountState> {
  return toBillingAccountState(
    await ok(
      api.PUT("/v1/billing/plan", {
        body: {
          plan: selection.plan,
          terms_version: selection.terms_version as Schemas["TermsVersion"],
        },
      }),
    ),
  );
}

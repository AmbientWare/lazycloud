import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import type { BillingPreferences as Preferences } from "@/lib/api/schemas";
import { formatCostNanos } from "@/lib/money";
import {
  automaticReloadStatusQueryOptions,
  billingPreferencesQueryOptions,
  resumeAutomaticReload,
  saveBillingPreferences,
  usageBudgetQueryOptions,
} from "@/lib/queries/billing";
import { pricingCatalogQueryOptions } from "@/lib/queries/pricing";

import { AmountSelect } from "./AmountSelect";

export function BillingPreferences({ paymentMethodOnFile }: { paymentMethodOnFile: boolean }) {
  const query = useQuery(billingPreferencesQueryOptions());
  if (query.isPending) return <p role="status">Loading billing settings…</p>;
  if (query.error)
    return (
      <p role="alert" className="text-sm text-destructive">
        {query.error.message}
      </p>
    );
  return <PreferencesForm preferences={query.data} paymentMethodOnFile={paymentMethodOnFile} />;
}

function PreferencesForm({
  preferences,
  paymentMethodOnFile,
}: {
  preferences: Preferences;
  paymentMethodOnFile: boolean;
}) {
  const queryClient = useQueryClient();
  const pricing = useQuery(pricingCatalogQueryOptions());
  const status = useQuery(automaticReloadStatusQueryOptions());
  const budget = useQuery(usageBudgetQueryOptions());
  const id = useId();
  const [draft, setDraft] = useState<{
    enabled?: boolean;
    threshold?: string;
    amount?: string;
    usageLimit?: string;
  }>({});
  const enabled = draft.enabled ?? preferences.reload_enabled;
  const threshold = draft.threshold ?? String(preferences.reload_threshold_cents / 100);
  const amount = draft.amount ?? String(preferences.reload_amount_cents / 100);
  const usageLimit =
    draft.usageLimit ??
    (preferences.monthly_usage_limit_nanos === null
      ? ""
      : String(preferences.monthly_usage_limit_nanos / 1e9));
  const toCents = (value: string) =>
    /^\d+(\.\d{1,2})?$/.test(value) && Number.isSafeInteger(Math.round(Number(value) * 100))
      ? Math.round(Number(value) * 100)
      : NaN;
  const thresholdCents = toCents(threshold);
  const amountCents = toCents(amount);
  const usageLimitNanos = usageLimit === "" ? null : toCents(usageLimit) * 1e7;
  const terms = pricing.data?.credit_purchase;
  const valid =
    terms &&
    thresholdCents >= 0 &&
    thresholdCents <= terms.maximum_cents &&
    amountCents >= terms.minimum_cents &&
    amountCents <= terms.maximum_cents &&
    (usageLimitNanos === null || (Number.isSafeInteger(usageLimitNanos) && usageLimitNanos >= 0));
  const dirty =
    enabled !== preferences.reload_enabled ||
    thresholdCents !== preferences.reload_threshold_cents ||
    amountCents !== preferences.reload_amount_cents ||
    usageLimitNanos !== preferences.monthly_usage_limit_nanos;
  const save = useMutation({
    mutationFn: saveBillingPreferences,
    onSuccess: (value) => {
      queryClient.setQueryData(billingPreferencesQueryOptions().queryKey, value);
      void queryClient.invalidateQueries({
        queryKey: automaticReloadStatusQueryOptions().queryKey,
      });
      void queryClient.invalidateQueries({ queryKey: usageBudgetQueryOptions().queryKey });
      setDraft({});
    },
  });
  const resume = useMutation({
    mutationFn: resumeAutomaticReload,
    onSuccess: (value) =>
      queryClient.setQueryData(automaticReloadStatusQueryOptions().queryKey, value),
  });
  const busy = save.isPending || resume.isPending;
  const error = save.error ?? resume.error ?? pricing.error ?? status.error ?? budget.error;

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid && dirty && !busy)
          save.mutate({
            reload_enabled: enabled,
            reload_threshold_cents: thresholdCents,
            reload_amount_cents: amountCents,
            monthly_usage_limit_nanos: usageLimitNanos,
          });
      }}
    >
      <div className="grid gap-4 md:grid-cols-2 md:gap-6">
        <div className="min-w-0 space-y-3">
          <label className="flex items-center gap-2 text-sm font-medium">
            <Checkbox
              checked={enabled}
              disabled={busy || (!paymentMethodOnFile && !enabled)}
              onCheckedChange={(value) => {
                setDraft({ ...draft, enabled: value === true });
                save.reset();
              }}
            />
            Automatic reload
          </label>
          <div className="grid grid-cols-2 gap-3">
            {[
              {
                name: "threshold",
                label: "When balance reaches, USD",
                value: threshold,
                presets: [0, 5, 10, 20, 50, 100],
                minimum: 0,
              },
              {
                name: "amount",
                label: "Add, USD",
                value: amount,
                presets: [5, 10, 20, 50, 100, 250, 500, 1000],
                minimum: terms ? terms.minimum_cents / 100 : undefined,
              },
            ].map((field) => (
              <div key={field.name} className="flex min-w-0 flex-col gap-1">
                <label
                  htmlFor={`${id}-${field.name}`}
                  className="flex-1 text-xs text-muted-foreground"
                >
                  {field.label}
                </label>
                <AmountSelect
                  id={`${id}-${field.name}`}
                  label={field.label}
                  value={field.value}
                  presets={field.presets}
                  minimum={field.minimum}
                  maximum={terms ? terms.maximum_cents / 100 : undefined}
                  disabled={busy || !paymentMethodOnFile}
                  onChange={(value) => {
                    setDraft({ ...draft, [field.name]: value });
                    save.reset();
                  }}
                />
              </div>
            ))}
          </div>
          {status.data?.pause_reason ? (
            <div className="space-y-2">
              <p role="status" className="text-xs text-warning">
                Reload paused.{" "}
                {status.data.pause_reason === "action_required"
                  ? "Your bank needs authentication. Add credit through checkout or update your card."
                  : "Your payment was declined. Update your card before resuming."}
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy || !paymentMethodOnFile || !preferences.reload_enabled}
                onClick={() => resume.mutate()}
              >
                {resume.isPending ? "Resuming…" : "Resume reload"}
              </Button>
            </div>
          ) : status.data?.pending_purchase_id ? (
            <p role="status" className="text-xs">
              An automatic payment is processing.
            </p>
          ) : null}
        </div>
        <div className="min-w-0 space-y-3 border-t border-border pt-4 md:border-t-0 md:border-l md:pt-0 md:pl-6">
          <label htmlFor={`${id}-usageLimit`} className="text-sm font-medium">
            Monthly usage limit, USD
          </label>
          <AmountSelect
            id={`${id}-usageLimit`}
            label="Monthly usage limit, USD"
            value={usageLimit}
            presets={[0, 25, 50, 100, 250, 500, 1000]}
            allowNoLimit
            disabled={busy}
            onChange={(value) => {
              setDraft({ ...draft, usageLimit: value });
              save.reset();
            }}
          />
          {budget.data ? (
            <p className="text-xs text-muted-foreground">
              {formatCostNanos(budget.data.spent_nanos)} used this month.
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground">
            Set $0 to stop new work and transfers, or choose No limit.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-end gap-3 border-t border-border pt-3">
        {error ? (
          <p role="alert" className="mr-auto text-sm text-destructive">
            {error.message}
          </p>
        ) : null}
        {save.isSuccess ? (
          <p role="status" className="text-sm text-muted-foreground">
            Changes saved.
          </p>
        ) : null}
        <Button type="submit" size="sm" disabled={!valid || !dirty || busy}>
          {save.isPending ? "Saving…" : "Save changes"}
        </Button>
      </div>
    </form>
  );
}

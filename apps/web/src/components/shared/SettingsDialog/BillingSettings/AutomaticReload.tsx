import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { Panel } from "@/components/shared/Panel";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import type { BillingPreferences } from "@/lib/api/schemas";
import {
  automaticReloadStatusQueryOptions,
  billingPreferencesQueryOptions,
  resumeAutomaticReload,
  saveBillingPreferences,
} from "@/lib/queries/billing";
import { pricingCatalogQueryOptions } from "@/lib/queries/pricing";

export function AutomaticReload() {
  const queryClient = useQueryClient();
  const preferences = useQuery(billingPreferencesQueryOptions());
  const status = useQuery(automaticReloadStatusQueryOptions());
  const resume = useMutation({
    mutationFn: resumeAutomaticReload,
    onSuccess: (value) =>
      queryClient.setQueryData(automaticReloadStatusQueryOptions().queryKey, value),
  });
  return (
    <Panel title="Automatic reload">
      <div className="flex flex-col gap-3 p-4">
        <p className="text-sm text-muted-foreground">
          Charge your saved payment method when your credit balance reaches or falls below your
          threshold. Trial, subscription and purchased credit all count toward the balance.
        </p>
        {status.data ? (
          <p className="text-sm">
            ${(status.data.monthly_payment_committed_cents / 100).toFixed(2)} in automatic payments
            made or pending this UTC calendar month.
          </p>
        ) : null}
        {status.data?.pause_reason ? (
          <div className="flex flex-col items-start gap-2">
            <p role="status" className="text-sm text-warning">
              Automatic reload is paused.{" "}
              {status.data.pause_reason === "action_required"
                ? "Your bank requires authentication. Buy credit through checkout or update your saved card."
                : "Your payment was declined. Update your saved card before resuming."}
            </p>
            <Button
              variant="outline"
              disabled={resume.isPending || !preferences.data?.reload_enabled}
              onClick={() => resume.mutate()}
            >
              {resume.isPending ? "Resuming…" : "Resume automatic reload"}
            </Button>
          </div>
        ) : status.data?.pending_purchase_id ? (
          <p role="status" className="text-sm">
            An automatic payment is processing.
          </p>
        ) : null}
        {preferences.data ? <ReloadForm preferences={preferences.data} /> : null}
        {preferences.isPending ? <p role="status">Loading reload settings…</p> : null}
        {preferences.error || status.error || resume.error ? (
          <p role="alert" className="text-sm text-destructive">
            {preferences.error?.message ?? status.error?.message ?? resume.error?.message}
          </p>
        ) : null}
      </div>
    </Panel>
  );
}

function ReloadForm({ preferences }: { preferences: BillingPreferences }) {
  const queryClient = useQueryClient();
  const pricing = useQuery(pricingCatalogQueryOptions());
  const id = useId();
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [threshold, setThreshold] = useState<string | null>(null);
  const [amount, setAmount] = useState<string | null>(null);
  const [limit, setLimit] = useState<string | null>(null);
  const currentThreshold = threshold ?? String(preferences.reload_threshold_cents / 100);
  const currentAmount = amount ?? String(preferences.reload_amount_cents / 100);
  const currentLimit =
    limit ??
    (preferences.reload_monthly_payment_limit_cents === null
      ? ""
      : String(preferences.reload_monthly_payment_limit_cents / 100));
  const toCents = (value: string) =>
    /^\d+(\.\d{1,2})?$/.test(value) && Number.isSafeInteger(Math.round(Number(value) * 100))
      ? Math.round(Number(value) * 100)
      : NaN;
  const thresholdCents = toCents(currentThreshold);
  const amountCents = toCents(currentAmount);
  const limitCents = currentLimit === "" ? null : toCents(currentLimit);
  const terms = pricing.data?.credit_purchase;
  const valid =
    terms &&
    thresholdCents >= 0 &&
    thresholdCents <= terms.maximum_cents &&
    amountCents >= terms.minimum_cents &&
    amountCents <= terms.maximum_cents &&
    (limitCents === null || limitCents >= 0);
  const save = useMutation({
    mutationFn: saveBillingPreferences,
    onSuccess: (value) => {
      queryClient.setQueryData(billingPreferencesQueryOptions().queryKey, value);
      void queryClient.invalidateQueries({
        queryKey: automaticReloadStatusQueryOptions().queryKey,
      });
      setEnabled(null);
      setThreshold(null);
      setAmount(null);
      setLimit(null);
    },
  });
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid && !save.isPending)
          save.mutate({
            ...preferences,
            reload_enabled: enabled ?? preferences.reload_enabled,
            reload_threshold_cents: thresholdCents,
            reload_amount_cents: amountCents,
            reload_monthly_payment_limit_cents: limitCents,
          });
      }}
    >
      <label className="flex items-center gap-2 text-sm">
        <Checkbox
          checked={enabled ?? preferences.reload_enabled}
          disabled={save.isPending}
          onCheckedChange={(value) => {
            setEnabled(value === true);
            save.reset();
          }}
        />
        Enable automatic reload
      </label>
      {[
        {
          name: "threshold",
          label: "Reload below, USD",
          value: currentThreshold,
          set: setThreshold,
        },
        { name: "amount", label: "Amount to add, USD", value: currentAmount, set: setAmount },
        {
          name: "limit",
          label: "Monthly automatic payment limit, USD",
          value: currentLimit,
          set: setLimit,
        },
      ].map((field) => (
        <div key={field.name} className="flex flex-col gap-1">
          <label htmlFor={`${id}-${field.name}`} className="text-sm">
            {field.label}
          </label>
          <Input
            id={`${id}-${field.name}`}
            inputMode="decimal"
            value={field.value}
            disabled={save.isPending}
            placeholder={field.name === "limit" ? "No limit" : undefined}
            onChange={(event) => {
              field.set(event.target.value);
              save.reset();
            }}
          />
        </div>
      ))}
      <p className="text-xs text-muted-foreground">
        The payment limit applies to automatic reloads, including pending payments. Refunds do not
        reset it. Leave it blank for no limit. Payments already processing may finish after you
        disable reload. Your monthly usage limit is separate.
      </p>
      {terms ? (
        <p className="text-xs text-muted-foreground">
          Each reload must add ${terms.minimum_cents / 100} to ${terms.maximum_cents / 100}.
        </p>
      ) : null}
      <Button type="submit" disabled={!valid || save.isPending}>
        {save.isPending ? "Saving…" : "Save reload settings"}
      </Button>
      {save.error || pricing.error ? (
        <p role="alert" className="text-sm text-destructive">
          {save.error?.message ?? pricing.error?.message}
        </p>
      ) : null}
      {save.isSuccess ? (
        <p role="status" className="text-sm">
          Reload settings saved.
        </p>
      ) : null}
    </form>
  );
}

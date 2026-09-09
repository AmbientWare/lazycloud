import { useMutation, useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import { Panel } from "@/components/shared/Panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { creditBalanceQueryOptions, purchaseCredit } from "@/lib/queries/billing";
import { formatCostNanos } from "@/lib/money";
import { pricingCatalogQueryOptions } from "@/lib/queries/pricing";

export function PrepaidCredit() {
  const pricing = useQuery(pricingCatalogQueryOptions());
  const balance = useQuery(creditBalanceQueryOptions());
  const [amount, setAmount] = useState<string | null>(null);
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID());
  const inputId = useId();
  const purchase = useMutation({ mutationFn: purchaseCredit });
  const terms = pricing.data?.credit_purchase;
  const dollars = amount ?? (terms ? String(terms.minimum_cents / 100) : "");
  const cents = Math.round(Number(dollars) * 100);
  const valid =
    terms &&
    /^\d+(\.\d{1,2})?$/.test(dollars) &&
    cents >= terms.minimum_cents &&
    cents <= terms.maximum_cents;

  return (
    <Panel title="Prepaid credit">
      <form
        className="flex flex-col gap-3 p-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (valid && balance.data?.ready && !purchase.isPending) {
            purchase.mutate({ requestKey, amountCents: cents });
          }
        }}
      >
        <p className="text-sm text-muted-foreground">
          Add credit for compute, storage and transfer. Purchased credit does not expire.
        </p>
        {balance.data?.ready ? (
          <div className="text-sm">
            <p>{formatCostNanos(balance.data.compute.available_nanos)} available for compute.</p>
            <p>
              {formatCostNanos(balance.data.storage_and_transfer.available_nanos)} of that is also
              available for storage and transfer.
            </p>
            <p className="text-muted-foreground">
              {formatCostNanos(balance.data.compute.held_nanos)} reserved for running or queued
              work.
            </p>
            {balance.data.compute.debt_nanos > 0 ? (
              <p className="text-destructive">
                Outstanding balance: {formatCostNanos(balance.data.compute.debt_nanos)}. Add credit
                to resume work.
              </p>
            ) : null}
          </div>
        ) : balance.isPending ? (
          <p role="status">Loading credit balance…</p>
        ) : !balance.error ? (
          <p role="status">Credit purchases are not available for this account yet.</p>
        ) : null}
        <label htmlFor={inputId} className="text-sm">
          Amount in USD
        </label>
        <div className="flex gap-2">
          <Input
            id={inputId}
            inputMode="decimal"
            value={dollars}
            disabled={!terms || purchase.isPending}
            onChange={(event) => {
              setAmount(event.target.value);
              setRequestKey(crypto.randomUUID());
              purchase.reset();
            }}
          />
          <Button type="submit" disabled={!valid || !balance.data?.ready || purchase.isPending}>
            {purchase.isPending ? "Opening checkout…" : "Add credit"}
          </Button>
        </div>
        {terms && (
          <p className="text-xs text-muted-foreground">
            ${terms.minimum_cents / 100}–${terms.maximum_cents / 100} per purchase.
          </p>
        )}
        {purchase.error || pricing.error || balance.error ? (
          <p role="alert" className="text-sm text-destructive">
            {purchase.error?.message ?? pricing.error?.message ?? balance.error?.message}
          </p>
        ) : null}
        {purchase.data && !purchase.data.checkout_url ? (
          <p role="status" className="text-sm">
            {purchase.data.status === "succeeded"
              ? "Credit added."
              : `Payment ${purchase.data.status.replace("_", " ")}.`}
          </p>
        ) : null}
      </form>
    </Panel>
  );
}

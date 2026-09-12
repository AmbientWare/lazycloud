import { useId, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { creditBalanceQueryOptions, purchaseCredit } from "@/lib/queries/billing";
import { formatCostNanos } from "@/lib/money";
import { pricingCatalogQueryOptions } from "@/lib/queries/pricing";

import { AmountSelect } from "./AmountSelect";

export function PrepaidCredit({ paymentMethodOnFile }: { paymentMethodOnFile: boolean }) {
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
    paymentMethodOnFile &&
    terms &&
    /^\d+(\.\d{1,2})?$/.test(dollars) &&
    cents >= terms.minimum_cents &&
    cents <= terms.maximum_cents;

  return (
    <section
      className="grid gap-4 border-b border-border pb-4 sm:grid-cols-[1fr_auto] sm:items-center"
      aria-label="Prepaid credit"
    >
      <div className="min-w-0">
        <h2 className="text-sm text-muted-foreground">Available balance</h2>
        {balance.data?.ready ? (
          <p className="mt-1 font-mono text-3xl font-medium tracking-tight" aria-live="polite">
            {formatCostNanos(balance.data.balance_nanos)}
          </p>
        ) : balance.isPending ? (
          <Skeleton className="my-2 h-8 w-28" aria-label="Loading balance" />
        ) : !balance.error ? (
          <p role="status" className="mt-1 text-sm">
            Your balance is not available yet.
          </p>
        ) : null}
        {balance.data?.ready && balance.data.balance_nanos <= 0 ? (
          <p className="mt-1 text-xs text-destructive">
            Add credit to resume work. New credit covers any negative balance first.
          </p>
        ) : null}
      </div>
      <form
        className="flex min-w-0 flex-col gap-1.5 sm:w-72"
        onSubmit={(event) => {
          event.preventDefault();
          if (valid && balance.data?.ready && !purchase.isPending) {
            purchase.mutate({ requestKey, amountCents: cents });
          }
        }}
      >
        <label htmlFor={inputId} className="text-sm">
          Amount in USD
        </label>
        <div className="flex items-start gap-2">
          <AmountSelect
            id={inputId}
            label="Amount in USD"
            value={dollars}
            presets={[5, 10, 20, 50, 100, 250, 500, 1000]}
            minimum={terms ? terms.minimum_cents / 100 : undefined}
            maximum={terms ? terms.maximum_cents / 100 : undefined}
            disabled={!paymentMethodOnFile || !terms || purchase.isPending}
            onChange={(value) => {
              setAmount(value);
              setRequestKey(crypto.randomUUID());
              purchase.reset();
            }}
          />
          <Button
            type="submit"
            className="shrink-0"
            disabled={!valid || !balance.data?.ready || purchase.isPending}
          >
            {purchase.isPending ? "Opening checkout…" : "Add credits"}
          </Button>
        </div>
        {terms && (
          <p className="text-xs text-muted-foreground">
            {formatCostNanos(terms.minimum_cents * 10_000_000)} to{" "}
            {formatCostNanos(terms.maximum_cents * 10_000_000)}. Purchased credit never expires.
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
    </section>
  );
}

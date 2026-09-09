import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { Panel } from "@/components/shared/Panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { BillingPreferences } from "@/lib/api/schemas";
import { formatCostNanos } from "@/lib/money";
import {
  billingPreferencesQueryOptions,
  saveBillingPreferences,
  usageBudgetQueryOptions,
} from "@/lib/queries/billing";

export function UsageBudget() {
  const preferences = useQuery(billingPreferencesQueryOptions());
  const budget = useQuery(usageBudgetQueryOptions());
  return (
    <Panel title="Monthly usage limit">
      <div className="flex flex-col gap-3 p-4">
        <p className="text-sm text-muted-foreground">
          Limit usage across your workspaces, including usage paid with included credit. The limit
          resets on the first of each month at midnight UTC. Subscription fees are separate.
        </p>
        {budget.data ? (
          <p className="text-sm">
            {formatCostNanos(budget.data.spent_nanos)} used this month.{" "}
            {formatCostNanos(budget.data.held_nanos)} reserved for work already admitted.
          </p>
        ) : null}
        {preferences.data ? <BudgetForm preferences={preferences.data} /> : null}
        {preferences.isPending ? <p role="status">Loading saved limit…</p> : null}
        {preferences.error || budget.error ? (
          <p role="alert" className="text-sm text-destructive">
            {preferences.error?.message ?? budget.error?.message}
          </p>
        ) : null}
      </div>
    </Panel>
  );
}

function BudgetForm({ preferences }: { preferences: BillingPreferences }) {
  const queryClient = useQueryClient();
  const inputId = useId();
  const [draft, setDraft] = useState<string | null>(null);
  const saved = preferences.monthly_usage_limit_nanos;
  const amount = draft ?? (saved === null ? "" : String(saved / 1e9));
  const nanos = amount === "" ? null : Math.round(Number(amount) * 1e9);
  const valid =
    nanos === null ||
    (/^\d+(\.\d{1,2})?$/.test(amount) && Number.isSafeInteger(nanos) && nanos >= 0);
  const save = useMutation({
    mutationFn: saveBillingPreferences,
    onSuccess: (value) => {
      queryClient.setQueryData(billingPreferencesQueryOptions().queryKey, value);
      void queryClient.invalidateQueries({ queryKey: usageBudgetQueryOptions().queryKey });
      setDraft(null);
    },
  });
  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid && !save.isPending) {
          save.mutate({ ...preferences, monthly_usage_limit_nanos: nanos });
        }
      }}
    >
      <label htmlFor={inputId} className="text-sm">
        Limit in USD
      </label>
      <div className="flex gap-2">
        <Input
          id={inputId}
          inputMode="decimal"
          value={amount}
          placeholder="No limit"
          disabled={save.isPending}
          onChange={(event) => {
            setDraft(event.target.value);
            save.reset();
          }}
        />
        <Button type="submit" disabled={!valid || save.isPending}>
          {save.isPending ? "Saving…" : "Save limit"}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Leave blank for no limit. Set $0 to block new compute. Running compute stops when its
        current funding expires. Stored data and existing transfer access can still incur charges.
      </p>
      {save.error ? (
        <p role="alert" className="text-sm text-destructive">
          {save.error.message}
        </p>
      ) : null}
      {save.isSuccess ? (
        <p role="status" className="text-sm">
          Limit saved.
        </p>
      ) : null}
    </form>
  );
}

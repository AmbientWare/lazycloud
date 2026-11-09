import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw } from "lucide-react";
import { useState } from "react";

import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import type { Schemas } from "@/lib/api/client";
import { formatCostNanos } from "@/lib/money";
import {
  adminAlertsQueryOptions,
  fleetSpendQueryOptions,
  setAdminAlerts,
} from "@/lib/queries/spend";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

const usd = (micros: number) => formatCostNanos(micros * 1000, "USD", 2);

/** A limit field's dollars as micros: undefined when blank, null when under a micro or not a number. */
function limitMicros(text: string): number | undefined | null {
  if (!text.trim()) return undefined;
  const micros = Math.round(Number(text) * 1e6);
  return Number.isFinite(micros) && micros >= 1 ? micros : null;
}

export function SpendSettings() {
  const client = useQueryClient();
  const spend = useQuery(fleetSpendQueryOptions());
  const alerts = useQuery(adminAlertsQueryOptions());
  const s = spend.data;
  const over = (spent: number, limit: number | undefined) =>
    limit !== undefined && spent > limit ? "text-destructive" : undefined;

  return (
    <Panel
      title="Spend"
      className="min-h-0 flex-1"
      contentClassName="flex flex-col overflow-y-auto"
      action={
        <Button
          size="sm"
          variant="ghost"
          disabled={spend.isFetching}
          onClick={() =>
            void client.invalidateQueries({ queryKey: accountQueryKeys.admin.spend() })
          }
        >
          <RefreshCw
            className={spend.isFetching ? "size-3.5 motion-safe:animate-spin" : "size-3.5"}
            aria-hidden="true"
          />
          Refresh
        </Button>
      }
    >
      <section className="border-b border-border p-4">
        {spend.error ? <PanelError message={spend.error.message} /> : null}
        {spend.isPending ? <Skeleton className="h-16" /> : null}
        {s ? (
          <FactGrid columns={4}>
            <Fact
              label="Now"
              mono
              value={
                <span className={over(s.hourly_micros, alerts.data?.hourly_limit_micros)}>
                  {usd(s.hourly_micros)}/h
                </span>
              }
            />
            <Fact
              label="24 hours"
              mono
              value={
                <span className={over(s.last_day_micros, alerts.data?.daily_limit_micros)}>
                  {usd(s.last_day_micros)}
                </span>
              }
            />
            <Fact
              label={`Running · ${s.running.hosts}`}
              mono
              value={`${usd(s.running.hourly_micros)}/h`}
            />
            <Fact
              label={`Stopped · ${s.stopped.hosts}`}
              mono
              value={`${usd(s.stopped.hourly_micros)}/h`}
            />
          </FactGrid>
        ) : null}
      </section>
      {alerts.error ? <PanelError message={alerts.error.message} /> : null}
      {alerts.isPending ? <Skeleton className="m-4 h-40" /> : null}
      {alerts.data ? <AlertsForm saved={alerts.data} /> : null}
    </Panel>
  );
}

function AlertsForm({ saved }: { saved: Schemas["AdminAlerts"] }) {
  const client = useQueryClient();
  const text = (micros: number | undefined) => (micros === undefined ? "" : String(micros / 1e6));
  const [email, setEmail] = useState(saved.email);
  const [hourly, setHourly] = useState(text(saved.hourly_limit_micros));
  const [daily, setDaily] = useState(text(saved.daily_limit_micros));
  const next = {
    email: email.trim(),
    hourly_limit_micros: limitMicros(hourly),
    daily_limit_micros: limitMicros(daily),
  };
  const invalid = next.hourly_limit_micros === null || next.daily_limit_micros === null;
  const changed =
    next.email !== saved.email ||
    next.hourly_limit_micros !== saved.hourly_limit_micros ||
    next.daily_limit_micros !== saved.daily_limit_micros;

  const save = useMutation({
    mutationFn: () =>
      setAdminAlerts({
        email: next.email,
        hourly_limit_micros: next.hourly_limit_micros ?? undefined,
        daily_limit_micros: next.daily_limit_micros ?? undefined,
      }),
    onSuccess: (data) => client.setQueryData(accountQueryKeys.admin.alerts(), data),
  });

  const fields = [
    { label: "Alert email", value: email, set: setEmail, placeholder: "you@example.com" },
    { label: "Limit $/h", value: hourly, set: setHourly, placeholder: "None", limit: true },
    { label: "Limit $/day", value: daily, set: setDaily, placeholder: "None", limit: true },
  ];

  return (
    <form
      aria-label="Alerts"
      className="grid gap-3 p-4 sm:grid-cols-3"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      {fields.map((field) => (
        <label key={field.label} className="grid gap-1.5">
          <span className="micro-label">{field.label}</span>
          <Input
            type={field.limit ? "text" : "email"}
            inputMode={field.limit ? "decimal" : undefined}
            value={field.value}
            disabled={save.isPending}
            onChange={(event) => field.set(event.target.value)}
            placeholder={field.placeholder}
            aria-invalid={field.limit && limitMicros(field.value) === null}
            className={field.limit ? "h-8 bg-background font-mono" : "h-8 bg-background"}
          />
        </label>
      ))}
      <div className="flex items-center justify-end gap-3 sm:col-span-3">
        {save.error ? (
          <span className="text-xs text-destructive" role="alert">
            {save.error.message}
          </span>
        ) : save.isSuccess && !changed ? (
          <span className="text-xs text-muted-foreground" role="status">
            Saved
          </span>
        ) : null}
        <Button type="submit" size="sm" disabled={save.isPending || invalid || !changed}>
          {save.isPending ? <Loader2 className="animate-spin" /> : null}
          Save
        </Button>
      </div>
    </form>
  );
}

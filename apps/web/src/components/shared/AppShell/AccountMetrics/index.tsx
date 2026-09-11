import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";

import { PanelErrorBoundary } from "@/components/shared/ErrorBoundary";
import { DrawerHeader } from "@/components/shared/DrawerHeader";
import { ShareBar } from "@/components/shared/ShareBar";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { shareLabel } from "@/lib/format";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { taskMetricsQueryOptions } from "@/lib/queries/tasks";
import { accountContainerCountsQueryOptions } from "@/lib/queries/account-metrics";
import { useWorkspace } from "@/lib/workspace-context";
import { cn } from "@/lib/utils";

import { ActivityPanel } from "./ActivityPanel";

const TASK_METRICS_HOURS = 24;

/**
 * The account's instruments: what it is holding now, and what has moved through
 * it.
 *
 * A drawer rather than a page because these are readings taken while doing
 * something else — the question is "is this account healthy right now", asked
 * without leaving whatever answered it.
 *
 * Account-scoped rather than workspace-scoped because that is the scope the
 * figures are compared against: the concurrency ceiling is a term of a plan and
 * a plan belongs to a payer, so somebody running dev, staging and prod reads one
 * set of readings rather than adding up their own.
 */
export function AccountMetricsDrawer({ onClose }: { onClose: () => void }) {
  return (
    <Sheet open onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent
        aria-describedby={undefined}
        aria-label="Account metrics"
        className="gap-0 bg-background max-sm:left-0 max-sm:right-0 max-sm:max-w-none max-sm:border-l-0 sm:max-w-2xl xl:max-w-3xl"
      >
        <DrawerHeader>
          <SheetTitle className="min-w-0 truncate">Account metrics</SheetTitle>
        </DrawerHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-3">
          <PanelErrorBoundary title="Could not display account metrics">
            <ReadingStrip />
          </PanelErrorBoundary>
          <PanelErrorBoundary title="Account activity could not be displayed">
            <ActivityPanel />
          </PanelErrorBoundary>
        </div>
      </SheetContent>
    </Sheet>
  );
}

/**
 * Four readings on one framed strip.
 *
 * One strip rather than four cards: these are read together, and hairlines
 * between cells carry the separation that four floating surfaces would spend a
 * whole row of chrome on.
 *
 * Unqualified cells read the account. The two task cells name a workspace
 * because that is the scope their reading has — the task summary is the same one
 * the app surfaces read, and a cell that quietly answered for one workspace
 * inside a strip labelled for the account would be the kind of figure somebody
 * makes a decision on and is wrong about.
 */
function ReadingStrip() {
  const { workspace } = useWorkspace();
  const held = useQuery(accountContainerCountsQueryOptions());
  const billing = useQuery(billingSummaryQueryOptions());
  const tasks = useQuery(taskMetricsQueryOptions(workspace.id, TASK_METRICS_HOURS));

  const entitlements = billing.data?.entitlements;
  const usage = billing.data?.usage;

  return (
    <section
      aria-label="Account metrics"
      className="panel grid shrink-0 grid-cols-2 overflow-hidden rounded-md sm:grid-cols-4"
    >
      <Reading
        label="Containers"
        className="border-b border-r border-border sm:border-b-0"
        query={held}
        value={held.data ? held.data.running + held.data.pending : undefined}
        detail={
          held.data
            ? `${held.data.running.toLocaleString()} running · ${held.data.pending.toLocaleString()} pending`
            : undefined
        }
      />
      <ConcurrencyReading
        className="border-b border-border sm:border-b-0 sm:border-r"
        query={billing}
        pools={[
          {
            label: "CPU",
            used: usage?.concurrent_cpu_containers ?? 0,
            limit: entitlements?.max_concurrent_cpu_containers ?? 0,
          },
          {
            label: "GPU",
            used: usage?.concurrent_gpus ?? 0,
            limit: entitlements?.max_concurrent_gpus ?? 0,
          },
        ]}
      />
      <Reading
        label="Tasks · 24h"
        className="border-r border-border"
        query={tasks}
        value={tasks.data?.total}
        detail={
          tasks.data
            ? `${tasks.data.completed.toLocaleString()} completed in ${workspace.name}`
            : undefined
        }
      />
      <Reading
        label="Failures · 24h"
        query={tasks}
        value={tasks.data?.failed}
        tone={tasks.data && tasks.data.failed > 0 ? "danger" : "neutral"}
        detail={
          tasks.data
            ? `${shareLabel(tasks.data.failure_rate)} of tasks in ${workspace.name}`
            : undefined
        }
      />
    </section>
  );
}

type ReadingQuery = {
  isPending: boolean;
  isError: boolean;
  error: Error | null;
};

/**
 * The chrome every cell shares: its label, and what it shows while the reading
 * cannot be taken.
 *
 * A reading that failed says so in the cell rather than showing a dash: a dash
 * is what an instrument reads when the answer is genuinely zero or absent, and
 * the two must not look the same.
 */
function ReadingCell({
  label,
  query,
  className,
  children,
}: {
  label: string;
  query: ReadingQuery;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("min-w-0 px-4 py-3", className)}>
      <div className="micro-label truncate">{label}</div>
      {query.isPending ? (
        <div className="mt-1.5 space-y-1.5" aria-hidden="true">
          <Skeleton className="h-4 w-14" />
          <Skeleton className="h-2.5 w-20" />
        </div>
      ) : query.isError ? (
        <>
          <div className="readout mt-1 truncate text-[15px] text-destructive">Unavailable</div>
          <p
            className="mt-1 truncate text-[11px] text-muted-foreground"
            title={query.error?.message}
          >
            {query.error?.message ?? "Could not load this metric"}
          </p>
        </>
      ) : (
        children
      )}
    </div>
  );
}

/** One cell of the strip, holding one figure. */
function Reading({
  label,
  value,
  reading,
  detail,
  tone = "neutral",
  meter,
  query,
  className,
}: {
  label: string;
  value?: number;
  /** The figure as written when it is not one count, such as `3 / 10` against a ceiling. */
  reading?: string;
  detail?: ReactNode;
  tone?: "neutral" | "danger";
  /**
   * Only a reading with a bound gets a bar, because only it has something to be
   * near. Its colour is a second reading of a fact the cell already states in
   * both numbers, never the only one.
   */
  meter?: { used: number; limit: number };
  query: ReadingQuery;
  className?: string;
}) {
  return (
    <ReadingCell label={label} query={query} className={className}>
      <div
        className={cn(
          "readout mt-1 truncate text-[15px]",
          tone === "danger" ? "text-destructive" : "text-foreground",
        )}
      >
        {reading ?? value?.toLocaleString() ?? "—"}
      </div>
      {meter && meter.limit > 0 ? (
        <ShareBar share={meter.used / meter.limit} tone="capacity" className="mt-1.5 w-full" />
      ) : null}
      {detail ? <p className="mt-1 truncate text-[11px] text-muted-foreground">{detail}</p> : null}
    </ReadingCell>
  );
}

/**
 * The two concurrency pools, one line each.
 *
 * They are bounded separately. A container counts against the CPU ceiling, or
 * against the GPU one by the number of cards it holds, so a single combined
 * figure would leave an account refused a GPU reading a number with room in it.
 * One cell rather than two, because they are one question asked twice.
 */
function ConcurrencyReading({
  pools,
  query,
  className,
}: {
  pools: readonly { label: string; used: number; limit: number }[];
  query: ReadingQuery;
  className?: string;
}) {
  return (
    <ReadingCell label="Concurrency" query={query} className={className}>
      {pools.map((pool) => (
        <div className="mt-1.5 first:mt-1" key={pool.label}>
          <div className="flex items-baseline justify-between gap-2">
            <span className="micro-label shrink-0">{pool.label}</span>
            <span className="readout min-w-0 truncate text-[15px]">
              {pool.used.toLocaleString()} / {pool.limit.toLocaleString()}
            </span>
          </div>
          {pool.limit > 0 ? (
            <ShareBar share={pool.used / pool.limit} tone="capacity" className="mt-1 w-full" />
          ) : null}
        </div>
      ))}
      <p className="mt-1 truncate text-[11px] text-muted-foreground">Plan limits</p>
    </ReadingCell>
  );
}

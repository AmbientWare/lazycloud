import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";

import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import type { UsageCostRow } from "@/lib/api/schemas";
import { shareLabel } from "@/lib/format";
import { selectInfiniteList } from "@/lib/queries/infinite-list";
import { accountCostsQueryOptions, type UsageCostWindow } from "@/lib/queries/usage";
import { cn } from "@/lib/utils";

import { AppWorkloadCosts } from "./AppWorkloadCosts";
import { RowFigures } from "./RowFigures";

/**
 * What each app cost over the range, dearest first, opening onto the workloads
 * inside it.
 *
 * One list rather than a switch between app, workload and task: nobody arrives
 * asking to see every workload the account holds ranked against each other. They
 * arrive asking which app the bill came from and then which part of that app,
 * which is a list that opens rather than a grouping to re-pick at the top of the
 * page.
 *
 * Read straight from the priced ledger, so a row here and a line on the invoice
 * are the same segments summed twice rather than two calculations that have to
 * be kept in agreement.
 */
export function AppCostAccordion({
  window,
  caption,
}: {
  window: UsageCostWindow;
  caption: string;
}) {
  const costs = useInfiniteQuery(accountCostsQueryOptions(window, { groupBy: "app" }));
  const { items: rows, nextCursor } = selectInfiniteList(costs.data, costs.hasNextPage, rowKey);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const total = costs.data?.pages[0]?.cost_nanos ?? 0;
  const currency = costs.data?.pages[0]?.currency ?? "USD";

  if (costs.isPending) {
    return <RowsSkeleton rows={6} height="h-9" />;
  }
  // A request that failed and a range nothing ran in are opposite answers, and
  // only one of them means the figures on this page are missing. Reported as an
  // empty range, a failure reads as reassurance.
  if (costs.isError) {
    return <PanelError message={costs.error.message} />;
  }
  if (rows.length === 0) {
    return <PanelEmpty message={`No apps were billed during ${caption}`} className="h-40" />;
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="divide-y divide-border/70">
        {rows.map((row) => {
          const key = rowKey(row);
          const open = openKey === key;
          const name = appName(row);
          const share = total > 0 ? row.cost_nanos / total : 0;
          return (
            <div key={key}>
              {row.app_id ? (
                <AppRow
                  row={row}
                  name={name}
                  open={open}
                  share={share}
                  currency={currency}
                  onToggle={(element) => {
                    setOpenKey(open ? null : key);
                    if (!open) {
                      requestAnimationFrame(() => {
                        requestAnimationFrame(() => element.scrollIntoView({ block: "nearest" }));
                      });
                    }
                  }}
                />
              ) : (
                <UnattributedRow row={row} share={share} currency={currency} />
              )}
              {open && row.app_id ? (
                <div
                  role="region"
                  aria-label={`${name} workload costs`}
                  className="border-t border-border bg-background/35"
                >
                  <AppWorkloadCosts
                    window={window}
                    appId={row.app_id}
                    appCostNanos={row.cost_nanos}
                    currency={currency}
                  />
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
      <InfiniteScrollBoundary
        nextCursor={nextCursor}
        loading={costs.isFetchingNextPage}
        error={costs.isError}
        onLoadMore={() => void costs.fetchNextPage()}
        resourceLabel="apps"
      />
    </div>
  );
}

function AppRow({
  row,
  name,
  open,
  share,
  currency,
  onToggle,
}: {
  row: UsageCostRow;
  name: string;
  open: boolean;
  share: number;
  currency: string;
  onToggle: (element: HTMLElement) => void;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      data-selected={open}
      className="interactive-row group flex w-full min-w-0 items-center gap-3 px-3 py-2.5 text-left"
      onClick={(event) => onToggle(event.currentTarget.parentElement ?? event.currentTarget)}
    >
      <ChevronRight
        className={cn(
          "interactive-row-indicator size-3.5 shrink-0 text-muted-foreground transition-transform",
          open && "rotate-90 text-brand",
        )}
      />
      <RowIdentity name={name} detail={workspaceLabel(row)} title={row.app_id} />
      <RowFigures
        share={share}
        label={`${shareLabel(share)} of spend over this range`}
        costNanos={row.cost_nanos}
        currency={currency}
      />
    </button>
  );
}

/**
 * Usage that reached no app: an image build, or work started before an app owned
 * it. It has a cost and nothing beneath it to open, so the row does not pretend
 * to — and it cannot be resolved to one workspace's workloads either, since the
 * empty app id is the same key in every workspace the account holds.
 */
function UnattributedRow({
  row,
  share,
  currency,
}: {
  row: UsageCostRow;
  share: number;
  currency: string;
}) {
  return (
    <div className="flex w-full min-w-0 items-center gap-3 px-3 py-2.5">
      <span className="size-3.5 shrink-0" aria-hidden="true" />
      {row.category === "image-build" ? (
        <RowIdentity name="Image builds" detail={`Builds · ${workspaceLabel(row)}`} />
      ) : (
        <RowIdentity name="Unattributed" detail={`No app · ${workspaceLabel(row)}`} />
      )}
      <RowFigures
        share={share}
        label={`${shareLabel(share)} of spend over this range`}
        costNanos={row.cost_nanos}
        currency={currency}
      />
    </div>
  );
}

function RowIdentity({ name, detail, title }: { name: string; detail: string; title?: string }) {
  return (
    <span className="flex min-w-0 flex-1 flex-col">
      <span className="truncate text-[13px] font-medium text-foreground" title={title}>
        {name}
      </span>
      <span className="truncate text-[11px] text-muted-foreground">{detail}</span>
    </span>
  );
}

/** The page spans every workspace the account is invoiced for, so a row that did
 * not say which one it belongs to would leave two apps of the same name
 * indistinguishable. */
function workspaceLabel(row: UsageCostRow): string {
  return row.workspace_name || row.workspace_id || "Workspace removed";
}

/**
 * An app with no name has since been deleted. The cost stays — it was incurred —
 * and the row says so rather than carrying a label invented to fill the column.
 */
function appName(row: UsageCostRow): string {
  return row.app_name || "App removed";
}

/**
 * The whole identity the row carries. Grouped by app, every row's workload and
 * task ids are empty, and two workspaces' unattributed spend shares the empty
 * app id — so anything less would collapse those rows into one another in both
 * the cross-page dedupe and React's reconciliation.
 */
function rowKey(row: UsageCostRow): string {
  return `${row.workspace_id}|${row.app_id}|${row.category}`;
}

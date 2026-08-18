import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";

import { Panel } from "@/components/shared/Panel";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { StatusChip } from "@/components/shared/StatusChip";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { countLabel } from "@/components/shared/WorkspacePage/countLabel";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Container, Deployment } from "@/lib/api/schemas";
import { exactTime, relativeTime } from "@/lib/format";

import { groupDeploymentsByWorkload } from "../-workloads/grouping";
import { formatKind } from "./app-detail-format";

export function AppWorkloadsSection({
  workspaceName,
  appId,
  deployments,
  containers,
  pending,
  error,
  nextCursor,
  loadingMore,
  loadMoreError,
  onLoadMore,
  continuationLabel,
}: {
  workspaceName: string;
  appId: string;
  deployments: Deployment[] | undefined;
  containers: Container[] | undefined;
  pending: boolean;
  error: string | undefined;
  nextCursor: string | undefined;
  loadingMore: boolean;
  loadMoreError: boolean;
  onLoadMore: () => void;
  continuationLabel: string;
}) {
  const [kind, setKind] = useState<string>();
  const groups = groupDeploymentsByWorkload(deployments, appId);
  // The kinds this app actually deploys, not the kinds one could. A filter
  // offering a kind nothing here has is an option whose only outcome is an
  // empty list.
  const kindsPresent = [...new Set(groups.map((group) => group.kind))].sort();
  const rows = kind ? groups.filter((group) => group.kind === kind) : groups;
  const continuation = (
    <InfiniteScrollBoundary
      nextCursor={nextCursor}
      loading={loadingMore}
      error={loadMoreError}
      onLoadMore={onLoadMore}
      resourceLabel={continuationLabel}
    />
  );

  return (
    <div
      role="region"
      aria-labelledby="app-workloads-heading"
      className="min-h-[26rem] lg:h-full lg:min-h-0"
    >
      <Panel
        title={<span id="app-workloads-heading">Workloads</span>}
        description={countLabel(groups.length, "deployed workload")}
        action={
          <Select
            value={kind ?? "all"}
            onValueChange={(next) => setKind(next === "all" ? undefined : next)}
          >
            <SelectTrigger aria-label="Type" size="sm" className="w-40 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent align="end">
              <SelectItem value="all">All types</SelectItem>
              {kindsPresent.map((option) => (
                <SelectItem key={option} value={option}>
                  {formatKind(option)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
        className="h-full"
        contentClassName="p-0"
      >
        {pending ? (
          <RowsSkeleton rows={4} height="h-14" />
        ) : error ? (
          <div className="flex min-h-48 items-center justify-center px-4 text-sm text-destructive">
            {error}
          </div>
        ) : groups.length === 0 ? (
          <PanelEmpty message="No deployed workloads for this app" className="min-h-48" />
        ) : rows.length === 0 ? (
          <div>
            <PanelEmpty message="No workloads match this type" className="min-h-48" />
            {continuation}
          </div>
        ) : (
          <div>
            <div
              className="sticky top-0 z-10 hidden grid-cols-[minmax(8rem,1fr)_4.5rem_6rem_5.75rem_6.25rem_1rem] gap-2 border-b border-border bg-card px-3 py-2 text-[10px] text-muted-foreground xl:grid"
              aria-hidden="true"
            >
              <span>Workload</span>
              <span>Version</span>
              <span>Containers</span>
              <span>Status</span>
              <span>Deployed</span>
              <span />
            </div>
            <div className="divide-y divide-border/80">
              {rows.map((group) => {
                const groupContainers = (containers ?? []).filter(
                  (container) => container.stub_id && group.stubIds.includes(container.stub_id),
                );
                const running = groupContainers.filter(
                  (container) => container.status === "running",
                ).length;
                return (
                  <Link
                    key={group.name}
                    to="/w/$workspace/apps/$appId/workloads/$name"
                    params={{ workspace: workspaceName, appId, name: group.name }}
                    className="interactive-row group grid min-w-0 gap-x-2 gap-y-2 px-3 py-3 xl:grid-cols-[minmax(8rem,1fr)_4.5rem_6rem_5.75rem_6.25rem_1rem] xl:items-center"
                  >
                    <span className="flex min-w-0 items-center gap-2.5">
                      <StubKindIcon kind={group.kind} className="size-3.5" />
                      <span className="min-w-0">
                        <span className="mono block truncate text-sm font-medium text-foreground">
                          {group.name}
                        </span>
                        <span className="block text-[11px] text-muted-foreground">
                          {formatKind(group.kind)}
                        </span>
                      </span>
                    </span>

                    <span className="hidden text-xs xl:block">
                      <span className="mono block text-foreground">v{group.latest.version}</span>
                    </span>
                    <span className="hidden text-xs xl:block">
                      <span className="mono block text-foreground">{running} running</span>
                    </span>
                    <span className="hidden xl:block">
                      <StatusChip
                        status={group.active ? "deployed" : "inactive"}
                        live={group.active}
                      />
                    </span>
                    <time
                      dateTime={group.latest.created_at}
                      title={exactTime(group.latest.created_at)}
                      className="hidden text-xs text-muted-foreground xl:block"
                    >
                      {relativeTime(group.latest.created_at)}
                    </time>

                    <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground xl:hidden">
                      <span className="mono text-foreground">v{group.latest.version}</span>
                      <span aria-hidden="true">·</span>
                      <span>{running} running</span>
                      <span aria-hidden="true">·</span>
                      <time
                        dateTime={group.latest.created_at}
                        title={exactTime(group.latest.created_at)}
                      >
                        {relativeTime(group.latest.created_at)}
                      </time>
                      <StatusChip
                        status={group.active ? "deployed" : "inactive"}
                        live={group.active}
                      />
                    </span>
                    <ChevronRight
                      className="interactive-row-indicator hidden size-3.5 text-muted-foreground transition-colors xl:block"
                      aria-hidden="true"
                    />
                  </Link>
                );
              })}
            </div>
            {continuation}
          </div>
        )}
      </Panel>
    </div>
  );
}

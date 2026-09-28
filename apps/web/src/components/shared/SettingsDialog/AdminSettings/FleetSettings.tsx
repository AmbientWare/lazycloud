import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, RefreshCw } from "lucide-react";
import { Fragment, useEffect, useState } from "react";

import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { FleetCapacity, FleetMarket, FleetNode, FleetState } from "@/lib/api/schemas";
import { fleetNodesQueryOptions, fleetSummaryQueryOptions } from "@/lib/queries/fleet";
import { selectInfiniteList } from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

const states: Record<FleetState, string> = {
  serving: "Serving",
  starting: "Starting",
  draining: "Draining",
  preparing: "Preparing reserve",
  stopping: "Stopping",
  unavailable: "Unavailable",
  failed: "Failed",
  terminating: "Terminating",
  stopped: "Stopped",
  hibernate_unverified: "Hibernation unverified",
  image_saved: "Hibernated",
};

const quantity = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

function Capacity({ value }: { value: FleetCapacity }) {
  return (
    <span className="font-mono text-[11px] tabular-nums">
      {quantity.format(value.cpu_millicores / 1000)} CPU ·{" "}
      {quantity.format(value.memory_mib / 1024)} GiB
      {value.gpu_count > 0 ? ` · ${value.gpu_count} GPU` : ""}
    </span>
  );
}

function SnapshotTime({ value }: { value: string }) {
  return <time dateTime={value}>{new Date(value).toLocaleString()}</time>;
}

export function FleetSettings() {
  const client = useQueryClient();
  const summary = useQuery(fleetSummaryQueryOptions());
  const inventory = useInfiniteQuery(fleetNodesQueryOptions());
  const nodes = selectInfiniteList(inventory.data, inventory.hasNextPage, (node) => node.id);
  const refreshing = summary.isFetching || inventory.isFetching;
  const release = summary.data?.release;
  const plan = summary.data?.plan;
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!plan) return;
    const timeout = window.setTimeout(
      () => setNow(Date.now()),
      Math.max(0, Date.parse(plan.expires_at) - Date.now()),
    );
    return () => window.clearTimeout(timeout);
  }, [plan]);
  const expired = plan !== null && plan !== undefined && now >= Date.parse(plan.expires_at);

  return (
    <Panel
      title="Fleet"
      description="Platform capacity and scheduler targets"
      className="min-h-0 flex-1"
      action={
        <Button
          size="sm"
          variant="ghost"
          disabled={refreshing}
          onClick={() =>
            void client.invalidateQueries({ queryKey: accountQueryKeys.admin.fleet.root() })
          }
        >
          <RefreshCw
            className={refreshing ? "size-3.5 animate-spin" : "size-3.5"}
            aria-hidden="true"
          />
          Refresh
        </Button>
      }
    >
      {summary.error ? <PanelError message={summary.error.message} /> : null}
      {summary.isPending ? (
        <Skeleton className="m-4 h-28" />
      ) : summary.data ? (
        <div className="space-y-5 p-4">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-medium">Worker rollout</h3>
              {release ? (
                <>
                  <span className="font-mono text-xs">v{release.version}</span>
                  <Badge tone={release.complete ? "success" : "warning"}>
                    {release.complete ? "Complete" : "In progress"}
                  </Badge>
                </>
              ) : (
                <span className="text-xs text-muted-foreground">No active release</span>
              )}
            </div>
            {release ? (
              <p className="text-xs text-muted-foreground">
                {Object.entries(release.phases)
                  .map(([phase, count]) => `${count} ${phase.replaceAll("_", " ")}`)
                  .join(" · ") || "No enrolled nodes"}
                {release.pending_capacity_owners > 0
                  ? ` · ${release.pending_capacity_owners} capacity groups pending`
                  : ""}
              </p>
            ) : null}
            <p className="text-[11px] text-muted-foreground">
              Checked <SnapshotTime value={summary.data.observed_at} />
            </p>
          </div>
          <section aria-label="Scheduler capacity">
            <h3 className="text-sm font-medium">Capacity</h3>
            {plan && !expired ? (
              <>
                <p className="mt-1 text-[11px] text-muted-foreground">
                  Planner snapshot <SnapshotTime value={plan.generated_at} />. Refresh to check for
                  changes.
                </p>
                <div className="mt-3 overflow-x-auto">
                  <CapacityTable markets={plan.markets} />
                </div>
                {plan.markets.length === 0 ? (
                  <p className="py-4 text-sm text-muted-foreground">
                    No capacity markets in this plan.
                  </p>
                ) : null}
              </>
            ) : (
              <p className="mt-2 text-sm text-warning" role="status">
                {expired
                  ? "This planner snapshot expired. Refresh to check for a current plan."
                  : "No current scheduler plan. Targets are unavailable until the scheduler publishes a plan for the active release."}
              </p>
            )}
          </section>
        </div>
      ) : null}
      <section aria-label="Fleet nodes" className="border-t border-border">
        <div className="space-y-1 px-4 py-3">
          <h3 className="text-sm font-medium">Nodes</h3>
          <p className="text-[11px] text-muted-foreground">
            Usable capacity after host reservations. Allocation includes container memory overhead.
          </p>
          {inventory.data?.pages[0] ? (
            <p className="text-[11px] text-muted-foreground">
              Checked <SnapshotTime value={inventory.data.pages[0].observed_at} />
            </p>
          ) : null}
        </div>
        {inventory.isPending ? <Skeleton className="m-4 h-24" /> : null}
        {inventory.error && !inventory.isFetchNextPageError ? (
          <PanelError message={inventory.error.message} />
        ) : null}
        {nodes.items.length > 0 ? (
          <NodeTable nodes={nodes.items} />
        ) : inventory.isSuccess ? (
          <p className="px-4 pb-4 text-sm text-muted-foreground">No platform nodes.</p>
        ) : null}
        <InfiniteScrollBoundary
          nextCursor={nodes.nextCursor}
          loading={inventory.isFetchingNextPage}
          error={inventory.isFetchNextPageError}
          onLoadMore={() => void inventory.fetchNextPage()}
          resourceLabel="nodes"
        />
      </section>
    </Panel>
  );
}

function CapacityTable({ markets }: { markets: FleetMarket[] }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Market</TableHead>
          <TableHead>Warm free</TableHead>
          <TableHead>Reserve ready</TableHead>
          <TableHead>Allocated</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {markets.map((market) => {
          const key = `${market.preemptible}:${market.gpu_type}`;
          const open = expanded === key;
          const label = `${market.preemptible ? "Spot" : "On-demand"} ${market.gpu_type || "CPU"}`;
          return (
            <Fragment key={key}>
              <TableRow>
                <TableCell>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="-ml-2 whitespace-nowrap"
                    aria-expanded={open}
                    aria-controls={open ? `fleet-market-${key}` : undefined}
                    onClick={() => setExpanded(open ? null : key)}
                  >
                    <ChevronRight
                      className={open ? "size-3 rotate-90" : "size-3"}
                      aria-hidden="true"
                    />
                    {label}
                  </Button>
                </TableCell>
                <TableCell className="whitespace-nowrap">
                  <Capacity value={market.warm_free} />
                  <div className="mt-1 text-[11px] text-muted-foreground">
                    Target <Capacity value={market.warm_target} />
                  </div>
                </TableCell>
                <TableCell className="whitespace-nowrap">
                  <Capacity value={market.reserve_ready} />
                  <div className="mt-1 text-[11px] text-muted-foreground">
                    Target <Capacity value={market.reserve_target} />
                  </div>
                </TableCell>
                <TableCell className="whitespace-nowrap">
                  <Capacity value={market.allocated} />
                </TableCell>
              </TableRow>
              {open ? (
                <TableRow id={`fleet-market-${key}`} className="bg-muted/20 hover:bg-muted/20">
                  <TableCell colSpan={4} className="p-3">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>State</TableHead>
                          <TableHead>Nodes</TableHead>
                          <TableHead>Capacity</TableHead>
                          <TableHead>Allocated</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {market.states.map((item) => (
                          <TableRow key={item.state}>
                            <TableCell>{states[item.state]}</TableCell>
                            <TableCell className="font-mono text-xs">{item.machines}</TableCell>
                            <TableCell>
                              <Capacity value={item.capacity} />
                            </TableCell>
                            <TableCell>
                              <Capacity value={item.allocated} />
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                    {market.states.length === 0 ? (
                      <p className="px-3 py-2 text-xs text-muted-foreground">
                        No nodes in this market.
                      </p>
                    ) : null}
                    {market.reason ? (
                      <p className="px-3 pt-2 text-xs text-muted-foreground">{market.reason}</p>
                    ) : null}
                  </TableCell>
                </TableRow>
              ) : null}
            </Fragment>
          );
        })}
      </TableBody>
    </Table>
  );
}

function NodeTable({ nodes }: { nodes: FleetNode[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Node</TableHead>
          <TableHead>State</TableHead>
          <TableHead>Capacity / allocated</TableHead>
          <TableHead>Containers</TableHead>
          <TableHead>Runtime</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {nodes.map((node) => (
          <TableRow key={node.id}>
            <TableCell>
              <div className="font-mono text-xs">
                {node.instance_id || node.machine_id || node.id}
              </div>
              <div className="mt-1 text-[11px] text-muted-foreground">
                {node.provider} · {node.region} · {node.instance_type || "Type pending"}
              </div>
              <div className="text-[11px] text-muted-foreground">
                {node.preemptible ? "Spot" : "On-demand"}
                {node.gpu_type ? ` · ${node.gpu_type}` : ""}
              </div>
            </TableCell>
            <TableCell>
              <Badge
                tone={
                  node.state === "failed" || node.state === "unavailable"
                    ? "danger"
                    : node.state === "hibernate_unverified" || node.state === "starting"
                      ? "warning"
                      : node.state === "serving"
                        ? "success"
                        : "muted"
                }
              >
                {states[node.state]}
              </Badge>
            </TableCell>
            <TableCell>
              <div>
                <Capacity value={node.capacity} />
              </div>
              <div className="mt-1 text-muted-foreground">
                <Capacity value={node.allocated} />
              </div>
            </TableCell>
            <TableCell className="font-mono text-xs">{node.containers}</TableCell>
            <TableCell className="text-xs">{node.ready ? "Ready" : "Not ready"}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

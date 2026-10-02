import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, RefreshCw } from "lucide-react";
import { Fragment, useEffect, useState } from "react";

import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { Schemas } from "@/lib/api/client";
import { fleetNodesQueryOptions, fleetSummaryQueryOptions } from "@/lib/queries/fleet";
import { selectInfiniteList } from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

type FleetCapacity = Schemas["FleetCapacity"];

const states: Record<Schemas["FleetState"], string> = {
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
  const expired = plan !== undefined && now >= Date.parse(plan.expires_at);

  return (
    <Panel
      title={
        <span className="flex items-center gap-3">
          Fleet
          {release ? (
            <span className="text-xs font-normal text-muted-foreground">
              v{release.version}
              {release.complete ? "" : " updating"}
            </span>
          ) : null}
        </span>
      }
      className="min-h-0 flex-1"
      contentClassName="flex flex-col overflow-hidden"
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
            className={refreshing ? "size-3.5 motion-safe:animate-spin" : "size-3.5"}
            aria-hidden="true"
          />
          Refresh
        </Button>
      }
    >
      <Tabs defaultValue="capacity" className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <TabsList aria-label="Fleet" className="shrink-0 justify-start px-3">
          <TabsTrigger value="capacity">Capacity</TabsTrigger>
          <TabsTrigger value="nodes">Nodes</TabsTrigger>
        </TabsList>
        <TabsContent value="capacity" className="min-h-0 flex-1 overflow-auto">
          {summary.error ? <PanelError message={summary.error.message} /> : null}
          {summary.isPending ? (
            <Skeleton className="m-4 h-28" />
          ) : summary.data ? (
            plan && !expired ? (
              plan.markets.length > 0 ? (
                <CapacityTable markets={plan.markets} />
              ) : (
                <p className="p-4 text-sm text-muted-foreground">No capacity markets.</p>
              )
            ) : (
              <p className="p-4 text-sm text-warning" role="status">
                {expired
                  ? "Capacity data expired. Refresh to update."
                  : "Capacity data unavailable. Refresh to try again."}
              </p>
            )
          ) : null}
        </TabsContent>
        <TabsContent value="nodes" className="min-h-0 flex-1 overflow-auto">
          {inventory.isPending ? <Skeleton className="m-4 h-24" /> : null}
          {inventory.error && !inventory.isFetchNextPageError ? (
            <PanelError message={inventory.error.message} />
          ) : null}
          {nodes.items.length > 0 ? (
            <NodeTable nodes={nodes.items} />
          ) : inventory.isSuccess ? (
            <p className="p-4 text-sm text-muted-foreground">No platform nodes.</p>
          ) : null}
          <InfiniteScrollBoundary
            nextCursor={nodes.nextCursor}
            loading={inventory.isFetchingNextPage}
            error={inventory.isFetchNextPageError}
            onLoadMore={() => void inventory.fetchNextPage()}
            resourceLabel="nodes"
          />
        </TabsContent>
      </Tabs>
    </Panel>
  );
}

function CapacityTable({ markets }: { markets: Schemas["FleetMarket"][] }) {
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
                      <TableHeader className="static">
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

function NodeTable({ nodes }: { nodes: Schemas["FleetNode"][] }) {
  return (
    <Table className="min-w-[42rem]">
      <TableHeader>
        <TableRow>
          <TableHead>Node</TableHead>
          <TableHead>State</TableHead>
          <TableHead>Capacity / allocated</TableHead>
          <TableHead>Containers</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {nodes.map((node) => (
          <TableRow key={node.id}>
            <TableCell>
              <div className="font-mono text-xs whitespace-nowrap">
                {node.instance_id || node.machine_id || node.id}
              </div>
              <div className="mt-1 flex flex-wrap gap-x-2 text-[11px] text-muted-foreground">
                <span>{node.region}</span>
                <span>{node.instance_type || "Type pending"}</span>
                <span>{node.preemptible ? "Spot" : "On-demand"}</span>
                {node.gpu_type ? <span>{node.gpu_type}</span> : null}
              </div>
            </TableCell>
            <TableCell>
              <span
                className={
                  node.state === "failed" || node.state === "unavailable"
                    ? "text-xs text-destructive"
                    : node.state === "hibernate_unverified"
                      ? "text-xs text-warning"
                      : "text-xs"
                }
              >
                {states[node.state]}
              </span>
            </TableCell>
            <TableCell className="whitespace-nowrap">
              <div>
                <Capacity value={node.capacity} />
              </div>
              <div className="mt-1 text-muted-foreground">
                <Capacity value={node.allocated} />
              </div>
            </TableCell>
            <TableCell className="font-mono text-xs">{node.containers}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

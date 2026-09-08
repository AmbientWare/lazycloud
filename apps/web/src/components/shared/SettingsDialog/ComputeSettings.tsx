import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Cloud, CloudCog, Plus, Server } from "lucide-react";

import {
  awsConnectionIsRemoving,
  awsConnectionIsUsable,
  awsConnectionPresentation,
} from "./AwsConnectionDialog/lifecycle";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import type { AwsConnection, CustomerComputeInstance, UnitMachine } from "@/lib/api/schemas";
import {
  awsConnectionQueryOptions,
  computeInstancesQueryOptions,
  machinesQueryOptions,
} from "@/lib/queries/compute";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { cn } from "@/lib/utils";

import { AwsConnectionDialog } from "./AwsConnectionDialog";
import { JoinMachineDialog } from "./JoinMachineDialog";

export function ComputeSettings({ onUpgrade }: { onUpgrade: () => void }) {
  const connection = useQuery(awsConnectionQueryOptions());
  const billing = useQuery(billingSummaryQueryOptions());
  const instances = useQuery(computeInstancesQueryOptions());
  const machines = useQuery(machinesQueryOptions());
  const [expandedProvider, setExpandedProvider] = useState<"aws" | null>(null);
  const [awsDialogOpen, setAwsDialogOpen] = useState(false);
  const [joinDialogOpen, setJoinDialogOpen] = useState(false);
  const loadError = connection.error ?? instances.error;

  if (connection.isPending || instances.isPending || billing.isPending) {
    return <SettingsSkeleton />;
  }

  if (loadError) {
    return (
      <Panel title="Compute">
        <PanelError message={loadError.message} />
      </Panel>
    );
  }

  const awsInstances = (instances.data?.data ?? []).filter(
    (instance) => instance.provider === "aws",
  );
  const selfHostedMachines = machines.data?.data ?? [];

  return (
    <div className="flex min-h-full flex-col gap-5 pb-1">
      <ConnectedCloudsPanel
        connection={connection.data ?? null}
        instances={awsInstances}
        expanded={expandedProvider === "aws"}
        onToggle={() => setExpandedProvider((current) => (current === "aws" ? null : "aws"))}
        onManageAws={() => setAwsDialogOpen(true)}
        connectedCloudEnabled={billing.data?.entitlements?.connected_cloud ?? false}
        billingError={billing.error}
        onUpgrade={onUpgrade}
      />
      <SelfHostedPanel
        machines={selfHostedMachines}
        loading={machines.isPending}
        error={machines.error}
        onJoin={() => setJoinDialogOpen(true)}
      />
      <AwsConnectionDialog
        connection={connection.data ?? null}
        open={awsDialogOpen}
        onOpenChange={setAwsDialogOpen}
      />
      <JoinMachineDialog open={joinDialogOpen} onOpenChange={setJoinDialogOpen} />
    </div>
  );
}

function ConnectedCloudsPanel({
  connection,
  instances,
  expanded,
  onToggle,
  onManageAws,
  connectedCloudEnabled,
  billingError,
  onUpgrade,
}: {
  connection: AwsConnection | null;
  instances: CustomerComputeInstance[];
  expanded: boolean;
  onToggle: () => void;
  onManageAws: () => void;
  connectedCloudEnabled: boolean;
  billingError: Error | null;
  onUpgrade: () => void;
}) {
  const usable = connection ? awsConnectionIsUsable(connection) : false;
  return (
    <Panel
      title="Connected clouds"
      description="Available to every workspace in this account"
      action={
        <AddCloudMenu
          connection={connection}
          connectedCloudEnabled={connectedCloudEnabled}
          billingError={billingError}
          onSelectAws={onManageAws}
          onUpgrade={onUpgrade}
        />
      }
      className="min-h-[18rem]"
      contentClassName="overflow-y-auto"
    >
      {!connection ? (
        <PanelEmpty
          icon={CloudCog}
          message="No connected clouds"
          detail={
            billingError
              ? billingError.message
              : connectedCloudEnabled
                ? "Connect AWS. Capacity is created only when a workload uses AWS."
                : "Connected cloud accounts are available on the Team plan."
          }
          className="min-h-64 px-6"
        />
      ) : (
        <div>
          <CloudProviderRow
            connection={connection}
            instances={instances}
            expanded={expanded && usable}
            onToggle={onToggle}
            onManage={onManageAws}
          />
          {expanded && usable ? (
            <div
              role="region"
              aria-label="AWS connection details"
              className="border-t border-border bg-background/35"
            >
              <CloudInstances instances={instances} />
            </div>
          ) : null}
        </div>
      )}
    </Panel>
  );
}

function AddCloudMenu({
  connection,
  connectedCloudEnabled,
  billingError,
  onSelectAws,
  onUpgrade,
}: {
  connection: AwsConnection | null;
  connectedCloudEnabled: boolean;
  billingError: Error | null;
  onSelectAws: () => void;
  onUpgrade: () => void;
}) {
  if (!connection && billingError) return null;
  if (!connection && !connectedCloudEnabled) {
    return (
      <Button size="sm" onClick={onUpgrade}>
        Upgrade to Team
      </Button>
    );
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="sm" className="whitespace-nowrap">
          Add cloud
          <Plus />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuItem
          disabled={connection !== null}
          className="h-9 whitespace-nowrap"
          onSelect={onSelectAws}
        >
          <Cloud />
          <span className="min-w-0 flex-1 whitespace-nowrap">AWS</span>
          {connection ? (
            <span className="ml-auto text-[11px] text-muted-foreground">
              {awsConnectionPresentation(connection).label}
            </span>
          ) : null}
        </DropdownMenuItem>
        <DropdownMenuItem disabled className="h-9 whitespace-nowrap">
          <Cloud />
          <span className="min-w-0 flex-1 whitespace-nowrap">Google Cloud</span>
          <span className="ml-auto shrink-0 text-[11px]">Coming soon</span>
        </DropdownMenuItem>
        <DropdownMenuItem disabled className="h-9 whitespace-nowrap">
          <Cloud />
          <span className="min-w-0 flex-1 whitespace-nowrap">Microsoft Azure</span>
          <span className="ml-auto shrink-0 text-[11px]">Coming soon</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function CloudProviderRow({
  connection,
  instances,
  expanded,
  onToggle,
  onManage,
}: {
  connection: AwsConnection;
  instances: CustomerComputeInstance[];
  expanded: boolean;
  onToggle: () => void;
  onManage: () => void;
}) {
  const ready = instances.filter((instance) => instanceStatusReady(instance.status)).length;
  const pending = instances.filter((instance) => instanceStatusPending(instance.status)).length;
  const presentation = awsConnectionPresentation(connection);
  const usable = awsConnectionIsUsable(connection);
  const removing = awsConnectionIsRemoving(connection);
  const actionLabel = connectionActionLabel(connection);

  return (
    <div className="interactive-row group grid min-w-0 gap-3 px-4 py-3 sm:grid-cols-[minmax(15rem,1fr)_auto_minmax(9rem,auto)_auto] sm:items-center">
      <button
        type="button"
        aria-expanded={usable ? expanded : undefined}
        disabled={!usable}
        className="flex min-w-0 items-center gap-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default"
        onClick={onToggle}
      >
        <span className="flex size-4 shrink-0 items-center justify-center">
          {usable ? (
            <ChevronRight
              className={cn(
                "size-4 text-muted-foreground transition-transform",
                expanded && "rotate-90 text-brand",
              )}
              aria-hidden="true"
            />
          ) : null}
        </span>
        <span className="flex min-w-0 items-center gap-3">
          <span className="flex size-9 shrink-0 items-center justify-center border border-border bg-muted/30 text-brand">
            <Cloud className="size-4" aria-hidden="true" />
          </span>
          <span className="min-w-0">
            <span className="block text-sm font-medium">Amazon Web Services</span>
            <span className="mono mt-0.5 block truncate text-[11px] text-muted-foreground">
              {connection.account_id}
            </span>
          </span>
        </span>
      </button>
      <span className="sm:justify-self-end">
        <StatusChip status={presentation.label} live={presentation.live} />
      </span>
      {usable ? (
        <span className="min-w-24 text-left sm:text-right">
          <span className="mono block text-xs text-foreground">
            {instances.length} {instances.length === 1 ? "instance" : "instances"}
          </span>
          <span className="mt-0.5 block text-[10px] text-muted-foreground">
            {ready} ready{pending > 0 ? ` · ${pending} pending` : ""}
          </span>
        </span>
      ) : (
        <span className="max-w-72 text-xs leading-5 text-muted-foreground sm:text-right">
          {connection.detail}
        </span>
      )}
      {actionLabel && !removing ? (
        <Button type="button" variant="outline" size="sm" onClick={onManage}>
          {actionLabel}
        </Button>
      ) : (
        <span />
      )}
    </div>
  );
}

function CloudInstances({ instances }: { instances: CustomerComputeInstance[] }) {
  return (
    <section className="min-w-0 p-4">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium">AWS instances</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">Current capacity</p>
        </div>
        <span className="mono text-xs text-muted-foreground">{instances.length}</span>
      </div>
      {instances.length === 0 ? (
        <div className="border-y border-border px-4 py-8 text-center">
          <p className="text-sm font-medium">No AWS instances running</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            LazyCloud provisions capacity when a workload uses AWS.
          </p>
        </div>
      ) : (
        <ul
          aria-label="AWS compute instances"
          className="divide-y divide-border border-y border-border"
        >
          {instances.map((instance) => (
            <li
              key={instance.id}
              className="interactive-row grid gap-2 px-3 py-3 sm:grid-cols-[minmax(10rem,1fr)_auto_auto] sm:items-center"
            >
              <div className="min-w-0">
                <code className="mono block truncate text-xs">
                  {instance.machine_id || instance.id}
                </code>
                <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
                  {instance.instance_type ?? "AWS instance"} · {instance.region}
                </p>
              </div>
              <StatusChip status={instance.status} live={instanceStatusReady(instance.status)} />
              <div className="text-left text-[11px] text-muted-foreground sm:text-right">
                <p className="mono text-foreground">
                  {formatCpu(instance.cpu_millicores)} · {formatMemory(instance.memory_mb)}
                </p>
                {instance.bootstrap_failure_reason ? (
                  <p>{instance.bootstrap_failure_reason.replaceAll("_", " ")}</p>
                ) : null}
                {instance.bootstrap_failure_detail ? (
                  <p className="max-w-80 text-balance" title={instance.bootstrap_failure_detail}>
                    {instance.bootstrap_failure_detail}
                  </p>
                ) : null}
                <LiveRelativeTime value={instance.created_at} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function SelfHostedPanel({
  machines,
  loading,
  error,
  onJoin,
}: {
  machines: UnitMachine[];
  loading: boolean;
  error: Error | null;
  onJoin: () => void;
}) {
  return (
    <Panel
      title="Self-hosted machines"
      description="Hosts you connected"
      action={
        <Button size="sm" variant="outline" onClick={onJoin}>
          <Server />
          Join machine
        </Button>
      }
      className="shrink-0 lg:max-h-[15rem]"
      contentClassName="overflow-y-auto"
    >
      {loading ? (
        <RowsSkeleton rows={2} height="h-10" />
      ) : error ? (
        <PanelError message={error.message} />
      ) : machines.length === 0 ? (
        <PanelEmpty message="No self-hosted machines connected" className="px-4 py-6" />
      ) : (
        <ul aria-label="Self-hosted machines" className="divide-y divide-border">
          {machines.map((machine) => (
            <li
              key={machine.id}
              className="interactive-row grid gap-2 px-4 py-3 sm:grid-cols-[minmax(10rem,1fr)_auto_auto] sm:items-center"
            >
              <div className="min-w-0">
                <code className="mono block truncate text-xs">{machine.id}</code>
                <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
                  {machine.readiness_message}
                </p>
              </div>
              <StatusChip
                status={machine.readiness_phase}
                live={machine.readiness_phase === "ready"}
              />
              <p className="mono text-[11px] text-muted-foreground sm:text-right">
                {formatCpu(machine.cpu)} · {formatMemory(machine.memory)}
              </p>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function SettingsSkeleton() {
  return (
    <div className="flex min-h-full flex-col gap-3 lg:h-full">
      <Skeleton className="min-h-80 flex-1" />
      <Skeleton className="h-36 shrink-0" />
    </div>
  );
}

function connectionActionLabel(connection: AwsConnection): string | null {
  if (connection.available_actions.length === 0) return null;
  if (connection.available_actions.includes("authorize")) return "Resume setup";
  if (
    connection.available_actions.includes("retry") ||
    connection.phase === "degraded" ||
    connection.phase === "action_required"
  ) {
    return "Resolve";
  }
  if (connection.phase === "reconnect_pending" || connection.phase === "retiring_authorization") {
    return "Review";
  }
  return "Manage";
}

function instanceStatusReady(status: string): boolean {
  return ["ready", "available", "busy", "running"].includes(status);
}

function instanceStatusPending(status: string): boolean {
  return ["creating", "joining", "pending", "provisioning", "starting"].includes(status);
}

function formatCpu(millicores: number): string {
  if (millicores < 1000) return `${millicores}m`;
  const cores = millicores / 1000;
  return `${Number.isInteger(cores) ? cores.toFixed(0) : cores.toFixed(1)} cores`;
}

function formatMemory(mebibytes: number): string {
  if (mebibytes < 1024) return `${mebibytes} MiB`;
  return `${(mebibytes / 1024).toFixed(1)} GiB`;
}

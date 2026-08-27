import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Cloud,
  CloudCog,
  Loader2,
  Plus,
  Save,
  Server,
} from "lucide-react";

import {
  awsConnectionIsRemoving,
  awsConnectionIsUsable,
  awsConnectionPresentation,
} from "./AwsConnectionDialog/lifecycle";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type { AwsConnection, CustomerComputeInstance, UnitMachine } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";
import {
  awsConnectionQueryOptions,
  computeCatalogQueryOptions,
  computeInstancesQueryOptions,
  machinesQueryOptions,
} from "@/lib/queries/compute";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { cn } from "@/lib/utils";

import { AwsConnectionDialog } from "./AwsConnectionDialog";
import { JoinMachineDialog } from "./JoinMachineDialog";
import { useAwsComputeController } from "./AwsComputeForm/controller";
import { regionOptions, toggleAllowedRegion } from "./region-selection";

export function ComputeSettings({ onUpgrade }: { onUpgrade: () => void }) {
  const connection = useQuery(awsConnectionQueryOptions());
  const billing = useQuery(billingSummaryQueryOptions());
  const instances = useQuery(computeInstancesQueryOptions());
  const machines = useQuery(machinesQueryOptions());
  const [expandedProvider, setExpandedProvider] = useState<"aws" | null>(null);
  const [awsDialogOpen, setAwsDialogOpen] = useState(false);
  const [joinDialogOpen, setJoinDialogOpen] = useState(false);
  const catalog = useQuery(computeCatalogQueryOptions(expandedProvider === "aws"));
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
        catalogRegions={catalog.data?.data.map((item) => item.region) ?? []}
        catalogLoading={catalog.isPending && expandedProvider === "aws"}
        catalogError={catalog.error}
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
  catalogRegions,
  catalogLoading,
  catalogError,
  onToggle,
  onManageAws,
  connectedCloudEnabled,
  billingError,
  onUpgrade,
}: {
  connection: AwsConnection | null;
  instances: CustomerComputeInstance[];
  expanded: boolean;
  catalogRegions: string[];
  catalogLoading: boolean;
  catalogError: Error | null;
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
      description="Connected once for your account, and reachable from every workspace in it"
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
                ? "Connect an AWS account once. Capacity is provisioned there only when a workload requests AWS placement."
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
              <div className="grid min-h-0 lg:grid-cols-[minmax(22rem,0.9fr)_minmax(0,1.1fr)]">
                <section className="min-w-0 p-4 lg:border-r lg:border-border">
                  <div className="mb-4">
                    <h3 className="text-sm font-medium">Provisioning</h3>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      Instance limits and defaults
                    </p>
                  </div>
                  {catalogLoading ? (
                    <ProvisioningSkeleton />
                  ) : catalogError ? (
                    <p className="text-sm text-destructive" role="alert">
                      {catalogError.message}
                    </p>
                  ) : (
                    <AwsComputeForm regions={catalogRegions} />
                  )}
                </section>
                <CloudInstances instances={instances} />
              </div>
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

function AwsComputeForm({ regions }: { regions: string[] }) {
  const controller = useAwsComputeController();
  const [advanced, setAdvanced] = useState(false);

  if (controller.isLoading) return <ProvisioningSkeleton />;

  if (controller.loadError || !controller.draft) {
    return (
      <div className="space-y-3" role="alert">
        <p className="text-sm text-destructive">
          {controller.loadError?.message ?? "AWS provisioning settings are unavailable"}
        </p>
        <Button type="button" size="sm" variant="outline" onClick={controller.retryLoad}>
          Retry loading settings
        </Button>
      </div>
    );
  }

  const draft = controller.draft;

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        controller.save();
      }}
    >
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Default region" htmlFor="compute-default-region">
          <Select
            value={draft.defaultRegion}
            onValueChange={(value) => controller.updateField({ field: "defaultRegion", value })}
          >
            <SelectTrigger id="compute-default-region" className="w-full font-mono">
              <SelectValue />
            </SelectTrigger>
            <SelectContent align="start">
              {draft.allowedRegions.map((item) => (
                <SelectItem key={item} value={item}>
                  <span className="font-mono">{item}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <NumberField
          id="compute-max-cpu"
          label="CPU limit"
          value={draft.maxCpuInstances}
          min={draft.initialCpuWorkers}
          max={100}
          onChange={(value) => controller.updateField({ field: "maxCpuInstances", value })}
        />
        <NumberField
          id="compute-max-gpu"
          label="GPU limit"
          value={draft.maxGpuInstances}
          min={0}
          max={100}
          onChange={(value) => controller.updateField({ field: "maxGpuInstances", value })}
        />
      </div>

      <button
        type="button"
        className="flex w-full items-center justify-between border-y border-border py-2 text-left text-xs font-medium outline-none transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        aria-expanded={advanced}
        onClick={() => setAdvanced((value) => !value)}
      >
        Advanced AWS defaults
        <ChevronDown
          className={cn(
            "size-4 text-muted-foreground transition-transform",
            advanced && "rotate-180",
          )}
        />
      </button>

      {advanced ? (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Default instance type" htmlFor="compute-default-instance-type">
            <Input
              id="compute-default-instance-type"
              value={draft.defaultInstanceType}
              onChange={(event) =>
                controller.updateField({
                  field: "defaultInstanceType",
                  value: event.target.value,
                })
              }
              className="font-mono"
            />
          </Field>
          <NumberField
            id="compute-initial-cpu-workers"
            label="Initial CPU workers"
            value={draft.initialCpuWorkers}
            min={draft.minCpuWorkers}
            max={draft.maxCpuInstances}
            onChange={(value) => controller.updateField({ field: "initialCpuWorkers", value })}
          />
          <NumberField
            id="compute-min-cpu-workers"
            label="Minimum CPU workers"
            value={draft.minCpuWorkers}
            min={0}
            max={draft.initialCpuWorkers}
            onChange={(value) => controller.updateField({ field: "minCpuWorkers", value })}
          />
          <NumberField
            id="compute-min-free-cpu"
            label="Reserved free CPU"
            value={draft.minFreeCpuMillicores}
            min={0}
            suffix="millicores"
            onChange={(value) => controller.updateField({ field: "minFreeCpuMillicores", value })}
          />
          <NumberField
            id="compute-min-free-memory"
            label="Reserved free memory"
            value={draft.minFreeMemoryMib}
            min={0}
            suffix="MiB"
            onChange={(value) => controller.updateField({ field: "minFreeMemoryMib", value })}
          />
          <NumberField
            id="compute-idle-timeout"
            label="Idle timeout"
            value={draft.idleTimeoutSeconds}
            min={60}
            max={86_400}
            suffix="seconds"
            onChange={(value) => controller.updateField({ field: "idleTimeoutSeconds", value })}
          />
          <NumberField
            id="compute-root-volume"
            label="Root disk"
            value={draft.rootVolumeGib}
            min={50}
            max={2048}
            suffix="GiB"
            onChange={(value) => controller.updateField({ field: "rootVolumeGib", value })}
          />
          <Field label="Allowed regions" htmlFor="compute-allowed-regions">
            <RegionMultiSelect
              id="compute-allowed-regions"
              options={regionOptions(regions, draft.allowedRegions)}
              value={draft.allowedRegions}
              defaultRegion={draft.defaultRegion}
              onValueChange={(value) => controller.updateField({ field: "allowedRegions", value })}
            />
          </Field>
          <Field label="Allowed instance types" htmlFor="compute-allowed-types">
            <Input
              id="compute-allowed-types"
              value={draft.allowedInstanceTypes}
              onChange={(event) =>
                controller.updateField({
                  field: "allowedInstanceTypes",
                  value: event.target.value,
                })
              }
              placeholder="Automatic selection"
              className="font-mono"
            />
          </Field>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-end gap-3 border-t border-border pt-3">
        {controller.requiresReview ? (
          <div className="mr-auto space-y-2" role="alert">
            <p className="text-xs text-warning">
              Settings changed on the server. Review the merged fields before retrying.
            </p>
            <Button type="button" size="sm" variant="outline" onClick={controller.review}>
              Review changes
            </Button>
          </div>
        ) : controller.recoveryFailed ? (
          <div className="mr-auto space-y-2" role="alert">
            <p className="text-xs text-destructive">
              {controller.saveError?.message ?? "Could not reload the current settings"}
            </p>
            <Button type="button" size="sm" variant="outline" onClick={controller.retryLoad}>
              Retry loading settings
            </Button>
          </div>
        ) : controller.saveError ? (
          <p className="mr-auto text-xs text-destructive" role="alert">
            {controller.saveError.message}
          </p>
        ) : null}
        {controller.isSaved && !controller.isDirty ? (
          <p className="mr-auto text-xs text-success">Settings saved</p>
        ) : null}
        <Button type="submit" size="sm" disabled={!controller.canSave}>
          {controller.isSaving ? <Loader2 className="animate-spin" /> : <Save />}
          Save settings
        </Button>
      </div>
    </form>
  );
}

function CloudInstances({ instances }: { instances: CustomerComputeInstance[] }) {
  return (
    <section className="min-w-0 p-4">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium">AWS instances</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Capacity currently provisioned in this account
          </p>
        </div>
        <span className="mono text-xs text-muted-foreground">{instances.length}</span>
      </div>
      {instances.length === 0 ? (
        <div className="border-y border-border px-4 py-8 text-center">
          <p className="text-sm font-medium">No AWS instances running</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            The first workload placed on AWS will provision capacity within these limits.
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
                <time
                  dateTime={instance.created_at}
                  title={new Date(instance.created_at).toLocaleString()}
                >
                  {relativeTime(instance.created_at)}
                </time>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function RegionMultiSelect({
  id,
  options,
  value,
  defaultRegion,
  onValueChange,
}: {
  id: string;
  options: string[];
  value: string[];
  defaultRegion: string;
  onValueChange: (value: string[]) => void;
}) {
  const summary = value.length === 1 ? value[0] : `${value.length} regions`;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          className="h-9 w-full justify-between px-3 font-normal"
        >
          <span className="mono min-w-0 truncate text-left">{summary}</span>
          <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64 max-w-[calc(100vw-2rem)]">
        {options.map((option) => {
          const selected = value.includes(option);
          const isDefault = option === defaultRegion;
          return (
            <DropdownMenuCheckboxItem
              key={option}
              checked={selected}
              disabled={isDefault}
              onCheckedChange={() =>
                onValueChange(toggleAllowedRegion(value, option, defaultRegion))
              }
              onSelect={(event) => event.preventDefault()}
              className="h-9 gap-3"
            >
              <span className="mono min-w-0 flex-1 truncate">{option}</span>
              {isDefault ? (
                <span className="shrink-0 text-[10px] text-muted-foreground">Default</span>
              ) : null}
            </DropdownMenuCheckboxItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
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

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <label htmlFor={htmlFor} className="grid content-start gap-1.5">
      <span className="micro-label">{label}</span>
      {children}
    </label>
  );
}

function NumberField({
  id,
  label,
  value,
  min,
  max,
  suffix,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  min: number;
  max?: number;
  suffix?: string;
  onChange: (value: number) => void;
}) {
  return (
    <Field label={label} htmlFor={id}>
      <div className="relative">
        <Input
          id={id}
          type="number"
          min={min}
          max={max}
          value={value}
          onChange={(event) => onChange(clamp(Number(event.target.value), min, max))}
          className={cn("font-mono", suffix && "pr-16")}
        />
        {suffix ? (
          <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-[11px] text-muted-foreground">
            {suffix}
          </span>
        ) : null}
      </div>
    </Field>
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

function ProvisioningSkeleton() {
  return (
    <div className="space-y-3" aria-hidden="true">
      <div className="grid grid-cols-3 gap-3">
        <Skeleton className="h-14" />
        <Skeleton className="h-14" />
        <Skeleton className="h-14" />
      </div>
      <Skeleton className="h-9 w-full" />
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

function clamp(value: number, min: number, max?: number): number {
  if (!Number.isFinite(value)) return min;
  const bounded = Math.max(min, Math.round(value));
  return max === undefined ? bounded : Math.min(max, bounded);
}

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Copy } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { CronJob, Deployment, Stub } from "@/lib/api/schemas";
import { deploymentUrlQueryOptions } from "@/lib/queries/apps";
import { cronJobsQueryOptions } from "@/lib/queries/cron";
import { taskQueueStateQueryOptions } from "@/lib/queries/stubs";
import { formatDuration, relativeTime } from "@/lib/format";

import type { WorkloadGroup } from "./grouping";

export function WorkloadOperation({
  workspaceId,
  deployment,
  stub,
  group,
  isPublic,
  runningContainers,
}: {
  workspaceId: string;
  deployment: Deployment;
  stub: Stub | undefined;
  group: WorkloadGroup;
  isPublic: boolean;
  runningContainers: number;
}) {
  const kind = group.kind;
  const isInvokable = ["function", "endpoint", "asgi", "task-queue"].includes(kind);

  return (
    <div className="grid grid-cols-1 divide-y divide-border/80 lg:grid-cols-[minmax(0,1fr)_minmax(24rem,1.35fr)] lg:divide-x lg:divide-y-0">
      <div className="min-w-0 space-y-4 p-4">
        {isInvokable && group.active ? (
          <InvokeTarget workspaceId={workspaceId} deploymentId={deployment.id} />
        ) : isInvokable ? (
          <div className="text-sm text-muted-foreground">
            This version is stopped and is not accepting requests.
          </div>
        ) : null}
        {kind === "cron-job" ? (
          <ScheduleFacts workspaceId={workspaceId} group={group} />
        ) : null}
        {kind === "pod" ? (
          <PodFacts deployment={deployment} />
        ) : null}
        {kind === "task-queue" && stub ? (
          <QueueFacts
            workspaceId={workspaceId}
            stubId={stub.id}
          />
        ) : null}
        {(kind === "endpoint" || kind === "asgi") && (
          <HttpFacts deployment={deployment} isPublic={isPublic} />
        )}
      </div>
      <RuntimeFacts
        deployment={deployment}
        isPublic={isPublic}
        runningContainers={runningContainers}
        showRunning={kind !== "pod"}
        showExecutionLimits={kind !== "pod"}
        showAccess={kind !== "endpoint" && kind !== "asgi"}
      />
    </div>
  );
}

function InvokeTarget({ workspaceId, deploymentId }: { workspaceId: string; deploymentId: string }) {
  const query = useQuery(deploymentUrlQueryOptions(workspaceId, deploymentId));
  const [copied, setCopied] = useState(false);

  if (query.isPending) return <Skeleton className="h-8 w-full" />;
  if (query.isError) return <div className="text-sm text-destructive">{query.error.message}</div>;

  const copy = () => {
    void navigator.clipboard.writeText(query.data.url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1_500);
    });
  };

  return (
    <div>
      <div className="micro-label mb-1.5">Invoke URL</div>
      <div className="flex min-w-0 items-center gap-1.5">
        <code className="mono min-w-0 flex-1 truncate rounded bg-muted/60 px-2.5 py-1.5 text-xs">
          {query.data.url}
        </code>
        <Button variant="ghost" size="icon" onClick={copy} aria-label="Copy invoke URL">
          {copied ? <Check className="size-3.5 text-positive" /> : <Copy className="size-3.5" />}
        </Button>
      </div>
    </div>
  );
}

function RuntimeFacts({
  deployment,
  isPublic,
  runningContainers,
  showRunning,
  showExecutionLimits,
  showAccess,
}: {
  deployment: Deployment;
  isPublic: boolean;
  runningContainers: number;
  showRunning: boolean;
  showExecutionLimits: boolean;
  showAccess: boolean;
}) {
  const resources = deployment.spec.resources;
  return (
    <div className="grid grid-cols-2 content-start gap-x-4 gap-y-4 p-4 lg:grid-cols-5">
      <Fact label="Version" value={`v${deployment.version}`} />
      <Fact label="Placement" value={placementLabel(deployment)} />
      {showRunning ? <Fact label="Running" value={Intl.NumberFormat().format(runningContainers)} /> : null}
      {showExecutionLimits ? (
        <Fact label="Concurrency" value={Intl.NumberFormat().format(resources.concurrency)} />
      ) : null}
      <Fact label="Warm retention" value={retentionLabel(resources.keep_warm)} />
      {showExecutionLimits ? <Fact label="Timeout" value={durationLabel(resources.timeout_seconds)} /> : null}
      {showAccess ? <Fact label="Access" value={isPublic ? "Public" : "Token required"} /> : null}
      <Fact label="CPU" value={resources.cpu == null ? "Default" : `${resources.cpu} cores`} />
      <Fact label="Memory" value={resources.memory ?? "Default"} />
      {resources.gpu ? (
        <Fact
          label="GPU"
          value={`${resources.gpu}${resources.gpu_count > 1 ? ` x${resources.gpu_count}` : ""}`}
        />
      ) : null}
    </div>
  );
}

function placementLabel(deployment: Deployment): string {
  const placement = deployment.spec.placement;
  if (!placement) return "Not reported";
  if (placement.target === "managed") return "Managed";
  return placement.region ? `AWS · ${placement.region}` : "AWS";
}

function HttpFacts({ deployment, isPublic }: { deployment: Deployment; isPublic: boolean }) {
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-4">
      <Fact label="Route" value={deployment.spec.route || "/"} mono />
      <Fact
        label="Methods"
        value={deployment.kind === "asgi" ? "All" : deployment.spec.methods.join(", ") || "GET, POST"}
        mono
      />
      <Fact label="Authentication" value={isPublic ? "Public" : "Bearer token"} />
    </div>
  );
}

function QueueFacts({
  workspaceId,
  stubId,
}: {
  workspaceId: string;
  stubId: string;
}) {
  const state = useQuery(taskQueueStateQueryOptions(workspaceId, stubId));
  if (state.isPending) return <Skeleton className="h-24 w-full" />;
  if (state.isError) {
    return <div className="text-sm text-destructive">{state.error.message}</div>;
  }

  const consumers = state.data.active_consumers;
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-4 lg:grid-cols-4">
      <Fact label="Queue depth" value={Intl.NumberFormat().format(state.data.queue_depth)} />
      <Fact
        label="Oldest pending"
        value={state.data.oldest_pending_at ? relativeTime(state.data.oldest_pending_at) : "None"}
      />
      <Fact label="Consumers" value={`${Intl.NumberFormat().format(consumers)} active`} />
      <Fact
        label="Availability"
        value={`${Intl.NumberFormat().format(state.data.available_consumers)} available / ${Intl.NumberFormat().format(state.data.busy_consumers)} busy`}
      />
    </div>
  );
}

function PodFacts({
  deployment,
}: {
  deployment: Deployment;
}) {
  const ports = Object.entries(deployment.spec.ports);
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-4">
      <Fact
        label="Ports"
        value={ports.length ? ports.map(([name, port]) => `${name}:${port}`).join(", ") : "None"}
        mono
      />
      <Fact
        label="Command"
        value={deployment.spec.command.length ? deployment.spec.command.join(" ") : "Image default"}
        mono
      />
    </div>
  );
}

function ScheduleFacts({ workspaceId, group }: { workspaceId: string; group: WorkloadGroup }) {
  const cronJobs = useQuery(cronJobsQueryOptions(workspaceId));
  const deploymentIds = new Set(group.deployments.map((deployment) => deployment.id));
  const job: CronJob | undefined = (cronJobs.data?.cron_jobs ?? []).find((item) =>
    deploymentIds.has(item.deployment_id),
  );

  if (cronJobs.isPending) return <Skeleton className="h-24 w-full" />;
  if (cronJobs.isError) {
    return <div className="text-sm text-destructive">{cronJobs.error.message}</div>;
  }

  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-5">
      <Fact label="Schedule" value={job?.cron ?? "Not registered"} mono />
      <Fact label="Timezone" value="UTC" />
      <Fact
        label="Next run"
        value={
          job?.enabled === false
            ? "Disabled"
            : job?.next_run_at
              ? relativeTime(job.next_run_at)
              : "-"
        }
      />
      <Fact label="Last run" value={job?.last_run_at ? relativeTime(job.last_run_at) : "-"} />
    </div>
  );
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="micro-label mb-1">{label}</div>
      <div className={`${mono ? "mono" : ""} truncate text-sm`} title={value}>
        {value}
      </div>
    </div>
  );
}

function retentionLabel(seconds: number | null | undefined): string {
  if (seconds == null) return "Default";
  if (seconds === 0) return "Scale to zero";
  return formatDuration(seconds * 1_000);
}

function durationLabel(seconds: number | null | undefined): string {
  if (seconds == null || seconds === 0) return "No limit";
  return formatDuration(seconds * 1_000);
}

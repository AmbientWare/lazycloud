import { useQuery } from "@tanstack/react-query";

import { CopyButton } from "@/components/shared/CopyButton";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelError } from "@/components/shared/PanelError";
import { Skeleton } from "@/components/ui/skeleton";
import type { CronJob, Deployment } from "@/lib/api/schemas";
import { deploymentUrlQueryOptions } from "@/lib/queries/apps";
import { cronJobsQueryOptions } from "@/lib/queries/cron";

import type { WorkloadGroup } from "./grouping";

const INVOKABLE_KINDS = new Set(["function", "endpoint", "asgi"]);

export function WorkloadOperation({
  workspaceId,
  deployment,
  group,
}: {
  workspaceId: string;
  deployment: Deployment;
  group: WorkloadGroup;
}) {
  const kind = group.kind;
  // A schedule is a property of the workload, not a kind of it.
  const isScheduled = Boolean(group.latest.spec?.cron);

  return (
    <div className="flex min-w-0 flex-wrap items-start gap-x-8 gap-y-3">
      {INVOKABLE_KINDS.has(kind) ? (
        group.active ? (
          <InvokeTarget workspaceId={workspaceId} deploymentId={deployment.id} />
        ) : (
          <p className="text-sm text-muted-foreground">
            This version is stopped and cannot accept requests.
          </p>
        )
      ) : null}
      {isScheduled ? <ScheduleFacts workspaceId={workspaceId} group={group} /> : null}
      {kind === "pod" ? <PodFacts deployment={deployment} /> : null}
      {kind === "endpoint" || kind === "asgi" ? <HttpFacts deployment={deployment} /> : null}
    </div>
  );
}

function InvokeTarget({
  workspaceId,
  deploymentId,
}: {
  workspaceId: string;
  deploymentId: string;
}) {
  const query = useQuery(deploymentUrlQueryOptions(workspaceId, deploymentId));

  if (query.isPending) return <Skeleton className="h-14 w-full" />;
  if (query.isError) return <PanelError message={query.error.message} />;

  return (
    <div className="min-w-[18rem] flex-1 basis-[32rem]">
      <div className="micro-label mb-1.5">Invoke URL</div>
      <div className="flex min-w-0 items-start gap-1.5">
        <code className="mono min-w-0 flex-1 rounded-md bg-muted/60 px-2.5 py-1.5 text-xs break-all">
          {query.data.url}
        </code>
        <CopyButton value={query.data.url} label="invoke URL" className="shrink-0" />
      </div>
    </div>
  );
}

function HttpFacts({ deployment }: { deployment: Deployment }) {
  return (
    <FactGrid columns={2} className="max-w-xl">
      <Fact label="Route" value={deployment.spec.route || "/"} mono />
      <Fact
        label="Methods"
        value={
          deployment.kind === "asgi" ? "All" : deployment.spec.methods.join(", ") || "GET, POST"
        }
        mono
      />
    </FactGrid>
  );
}

function PodFacts({ deployment }: { deployment: Deployment }) {
  const ports = Object.entries(deployment.spec.ports);
  return (
    <FactGrid columns={2} className="max-w-3xl">
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
    </FactGrid>
  );
}

function ScheduleFacts({ workspaceId, group }: { workspaceId: string; group: WorkloadGroup }) {
  const cronJobs = useQuery(cronJobsQueryOptions(workspaceId));
  const deploymentIds = new Set(group.deployments.map((deployment) => deployment.id));
  const job: CronJob | undefined = (cronJobs.data?.cron_jobs ?? []).find((item) =>
    deploymentIds.has(item.deployment_id),
  );

  if (cronJobs.isPending) return <Skeleton className="h-12 w-full" />;
  if (cronJobs.isError) return <PanelError message={cronJobs.error.message} />;

  return (
    <FactGrid columns={4} className="max-w-3xl">
      <Fact label="Schedule" value={job?.cron ?? "Not registered"} mono />
      <Fact label="Timezone" value="UTC" />
      <Fact
        label="Next run"
        value={
          job?.enabled === false ? (
            "Disabled"
          ) : job?.next_run_at ? (
            <LiveRelativeTime value={job.next_run_at} />
          ) : (
            "-"
          )
        }
      />
      <Fact
        label="Last run"
        value={job?.last_run_at ? <LiveRelativeTime value={job.last_run_at} /> : "-"}
      />
    </FactGrid>
  );
}

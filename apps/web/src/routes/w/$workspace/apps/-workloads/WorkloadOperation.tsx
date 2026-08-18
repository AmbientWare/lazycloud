import { useQuery } from "@tanstack/react-query";

import { CopyButton } from "@/components/shared/CopyButton";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { Skeleton } from "@/components/ui/skeleton";
import type { CronJob, Deployment } from "@/lib/api/schemas";
import { deploymentUrlQueryOptions } from "@/lib/queries/apps";
import { cronJobsQueryOptions } from "@/lib/queries/cron";
import { relativeTime } from "@/lib/format";

import type { WorkloadGroup } from "./grouping";

const INVOKABLE_KINDS = new Set(["function", "endpoint", "asgi"]);

/**
 * Where the workload answers from, and what governs reaching it there.
 *
 * Only that: how the container is provisioned is reference a reader consults
 * once, so it sits in the inspector's configuration view rather than competing
 * with the address for the top of the page.
 */
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
    <div className="min-w-0 space-y-4 p-4">
      {INVOKABLE_KINDS.has(kind) ? (
        group.active ? (
          <InvokeTarget workspaceId={workspaceId} deploymentId={deployment.id} />
        ) : (
          <p className="text-sm text-muted-foreground">
            This version is stopped and is not accepting requests.
          </p>
        )
      ) : null}
      {isScheduled ? <ScheduleFacts workspaceId={workspaceId} group={group} /> : null}
      {kind === "pod" ? <PodFacts deployment={deployment} /> : null}
      {kind === "endpoint" || kind === "asgi" ? <HttpFacts deployment={deployment} /> : null}
    </div>
  );
}

/**
 * The address gets the page's whole measure. Fitted into a column beside other
 * readings it truncated to its hostname, which is the half a reader already
 * knows — and a copy control beside an elided value reads as copying the
 * elision.
 */
function InvokeTarget({
  workspaceId,
  deploymentId,
}: {
  workspaceId: string;
  deploymentId: string;
}) {
  const query = useQuery(deploymentUrlQueryOptions(workspaceId, deploymentId));

  if (query.isPending) return <Skeleton className="h-14 w-full" />;
  if (query.isError) {
    return (
      <p className="text-sm text-destructive" role="alert">
        {query.error.message}
      </p>
    );
  }

  return (
    <div className="min-w-0">
      <div className="micro-label mb-1.5">Invoke URL</div>
      <div className="flex min-w-0 items-start gap-1.5">
        <code className="mono min-w-0 flex-1 rounded bg-muted/60 px-2.5 py-1.5 text-xs break-all">
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
  if (cronJobs.isError) {
    return (
      <p className="text-sm text-destructive" role="alert">
        {cronJobs.error.message}
      </p>
    );
  }

  return (
    <FactGrid columns={4} className="max-w-3xl">
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
    </FactGrid>
  );
}

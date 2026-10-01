import { CopyButton } from "@/components/shared/CopyButton";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { invokeUrl, workloadRunning, type Workload } from "@/lib/queries/deployments";

export function WorkloadOperation({
  workspace,
  workload,
}: {
  workspace: string;
  workload: Workload;
}) {
  const { deployment, release } = workload;
  const kind = deployment.kind;

  return (
    <div className="flex min-w-0 flex-wrap items-start gap-x-8 gap-y-3">
      {workloadRunning(deployment) ? (
        <InvokeTarget url={invokeUrl(workspace, workload)} />
      ) : (
        <p className="text-sm text-muted-foreground">
          This version is stopped and cannot accept requests.
        </p>
      )}
      {/* A schedule is a property of the workload, not a kind of it. */}
      {release.spec.cron ? <ScheduleFacts workload={workload} /> : null}
      {kind === "endpoint" || kind === "asgi" ? <HttpFacts workload={workload} /> : null}
    </div>
  );
}

function InvokeTarget({ url }: { url: string }) {
  return (
    <div className="content-transition min-w-[18rem] flex-1 basis-[32rem]">
      <div className="micro-label mb-1.5">Invoke URL</div>
      <div className="flex min-w-0 items-start gap-1.5">
        <code className="mono min-w-0 flex-1 rounded-md bg-muted/60 px-2.5 py-1.5 text-xs break-all">
          {url}
        </code>
        <CopyButton value={url} label="invoke URL" className="shrink-0" />
      </div>
    </div>
  );
}

function HttpFacts({ workload }: { workload: Workload }) {
  const http = workload.release.spec.http;
  return (
    <FactGrid columns={2} className="max-w-xl">
      <Fact label="Route" value={http?.route || "/"} mono />
      <Fact
        label="Methods"
        value={
          workload.deployment.kind === "asgi" ? "All" : http?.methods?.join(", ") || "GET, POST"
        }
        mono
      />
    </FactGrid>
  );
}

function ScheduleFacts({ workload }: { workload: Workload }) {
  const { schedule, deployment } = workload;
  return (
    <FactGrid columns={4} className="content-transition max-w-3xl">
      <Fact label="Schedule" value={schedule?.cron ?? "Not registered"} mono />
      <Fact label="Timezone" value="UTC" />
      <Fact
        label="Next run"
        value={
          !schedule ? (
            "-"
          ) : deployment.state !== "active" ? (
            "Disabled"
          ) : (
            <LiveRelativeTime value={schedule.next_run_at} />
          )
        }
      />
      <Fact
        label="Last run"
        value={schedule?.last_run_at ? <LiveRelativeTime value={schedule.last_run_at} /> : "-"}
      />
    </FactGrid>
  );
}

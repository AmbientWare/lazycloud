import { CopyButton } from "@/components/shared/CopyButton";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import type { Schemas } from "@/lib/api/client";
import { invokeUrl, workloadRunning } from "@/lib/queries/deployments";

export function WorkloadOperation({
  workspace,
  detail,
}: {
  workspace: string;
  detail: Schemas["WorkloadDetail"];
}) {
  const { workload, release } = detail;
  const kind = workload.kind;
  const invokable = kind === "function" || kind === "endpoint" || kind === "asgi";

  return (
    <div className="flex min-w-0 flex-wrap items-start gap-x-8 gap-y-3">
      {invokable ? (
        workloadRunning(workload) ? (
          <InvokeTarget url={invokeUrl(workspace, detail)} />
        ) : (
          <p className="text-sm text-muted-foreground">
            This version is stopped and cannot accept requests.
          </p>
        )
      ) : null}
      {/* A schedule is a property of the workload, not a kind of it. */}
      {release.spec.cron ? <ScheduleFacts detail={detail} /> : null}
      {kind === "pod" ? <PodFacts pod={release.spec.pod} /> : null}
      {kind === "endpoint" || kind === "asgi" ? <HttpFacts detail={detail} /> : null}
    </div>
  );
}

function PodFacts({ pod }: { pod: Schemas["PodSpec"] | undefined }) {
  const ports = Object.entries(pod?.ports ?? {});
  const command = pod?.command ?? [];
  return (
    <FactGrid columns={2} className="max-w-3xl">
      <Fact
        label="Ports"
        value={ports.length ? ports.map(([name, port]) => `${name}:${port}`).join(", ") : "None"}
        mono
      />
      <Fact label="Command" value={command.length ? command.join(" ") : "Image default"} mono />
    </FactGrid>
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

function HttpFacts({ detail }: { detail: Schemas["WorkloadDetail"] }) {
  const http = detail.release.spec.http;
  return (
    <FactGrid columns={2} className="max-w-xl">
      <Fact label="Route" value={http?.route || "/"} mono />
      <Fact
        label="Methods"
        value={detail.workload.kind === "asgi" ? "All" : http?.methods?.join(", ") || "GET, POST"}
        mono
      />
    </FactGrid>
  );
}

function ScheduleFacts({ detail }: { detail: Schemas["WorkloadDetail"] }) {
  const { schedule, workload } = detail;
  return (
    <FactGrid columns={4} className="content-transition max-w-3xl">
      <Fact label="Schedule" value={schedule?.cron ?? "Not registered"} mono />
      <Fact label="Timezone" value="UTC" />
      <Fact
        label="Next run"
        value={
          !schedule ? (
            "-"
          ) : workload.state !== "active" ? (
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

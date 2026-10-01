import { CopyButton } from "@/components/shared/CopyButton";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import type { Schemas } from "@/lib/api/client";

import type { DeploymentManifest } from "./playground-form";

/** How a workload is reached and, for a scheduled one, when it runs. */
export function WorkloadOperation({
  fn,
  resource,
  active,
}: {
  fn: Schemas["Function"];
  resource: DeploymentManifest;
  active: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-start gap-x-8 gap-y-3">
      {!active ? (
        <p className="text-sm text-muted-foreground">
          This workload is stopped and cannot accept requests.
        </p>
      ) : resource.invoke_url ? (
        <InvokeTarget url={resource.invoke_url} />
      ) : null}
      {fn.schedule ? <ScheduleFacts schedule={fn.schedule} /> : null}
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

function ScheduleFacts({ schedule }: { schedule: Schemas["Schedule"] }) {
  return (
    <FactGrid columns={4} className="content-transition max-w-3xl">
      <Fact label="Schedule" value={schedule.cron} mono />
      <Fact label="Timezone" value={schedule.timezone} />
      <Fact label="Next run" value={<LiveRelativeTime value={schedule.next_run_at} />} />
      <Fact
        label="Last run"
        value={schedule.last_run_at ? <LiveRelativeTime value={schedule.last_run_at} /> : "-"}
      />
    </FactGrid>
  );
}

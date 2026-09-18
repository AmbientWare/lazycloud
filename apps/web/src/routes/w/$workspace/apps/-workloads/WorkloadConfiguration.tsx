import type { ReactNode } from "react";

import type { Deployment } from "@/lib/api/schemas";
import { formatDuration, resourceAllocation } from "@/lib/format";

export function WorkloadConfiguration({
  deployment,
  kind,
}: {
  deployment: Deployment;
  kind: string;
}) {
  const resources = deployment.spec.resources;
  // A Pod holds connections rather than executing tasks, so per-task
  // concurrency and timeout describe nothing it does.
  const executesTasks = kind !== "pod";

  return (
    <div className="@container min-w-0 space-y-5 p-4">
      <div className="grid min-w-0 gap-x-8 gap-y-5 @xl:grid-cols-2">
        <ConfigurationGroup title="Runtime">
          <ConfigurationFact label="CPU" value={resourceAllocation(resources.cpu, "vCPUs")} />
          <ConfigurationFact label="Memory" value={resourceAllocation(resources.memory)} />
          {resources.gpu.length > 0 ? (
            <ConfigurationFact
              label="GPU"
              value={`${resources.gpu.join(" → ")}${resources.gpu_count > 1 ? ` x${resources.gpu_count}` : ""}`}
            />
          ) : null}
          {deployment.spec.machine ? (
            <ConfigurationFact label="Machine" value={deployment.spec.machine} />
          ) : null}
          <ConfigurationFact label="Region" value={resources.region || "Automatic"} />
          {resources.availability_zone ? (
            <ConfigurationFact label="Availability zone" value={resources.availability_zone} />
          ) : null}
        </ConfigurationGroup>

        <ConfigurationGroup title="Execution">
          {executesTasks ? (
            <ConfigurationFact
              label="Concurrency"
              value={Intl.NumberFormat().format(resources.concurrency)}
            />
          ) : null}
          {executesTasks ? (
            <ConfigurationFact label="Timeout" value={timeoutLabel(resources.timeout_seconds)} />
          ) : null}
          <ConfigurationFact label="Keep warm" value={retentionLabel(resources.keep_warm)} />
        </ConfigurationGroup>
      </div>
    </div>
  );
}

function ConfigurationGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <h3 className="mb-2 text-sm font-medium text-foreground">{title}</h3>
      <dl className="space-y-2">{children}</dl>
    </section>
  );
}

function ConfigurationFact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="grid min-w-0 grid-cols-[minmax(7rem,2fr)_minmax(0,3fr)] items-baseline gap-3">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm break-words tabular-nums [overflow-wrap:anywhere]">{value}</dd>
    </div>
  );
}

/** `-1` is the container that never retires itself; the autoscaler removes it. */
function retentionLabel(seconds: number | null | undefined): string {
  if (seconds == null) return "Default";
  if (seconds < 0) return "Always warm";
  if (seconds === 0) return "Scale to zero";
  return formatDuration(seconds * 1_000);
}

function timeoutLabel(seconds: number | null | undefined): string {
  if (seconds == null || seconds === 0) return "No limit";
  return formatDuration(seconds * 1_000);
}

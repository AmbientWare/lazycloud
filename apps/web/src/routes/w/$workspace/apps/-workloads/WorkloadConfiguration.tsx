import type { ReactNode } from "react";

import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
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
    <div className="grid min-w-0 gap-x-8 gap-y-6 p-4 lg:grid-cols-2">
      <ConfigurationGroup title="Runtime">
        <Fact label="CPU" value={resourceAllocation(resources.cpu, "vCPUs")} />
        <Fact label="Memory" value={resourceAllocation(resources.memory)} />
        {resources.gpu.length > 0 ? (
          <Fact
            label="GPU"
            value={`${resources.gpu.join(" → ")}${resources.gpu_count > 1 ? ` x${resources.gpu_count}` : ""}`}
          />
        ) : null}
        <Fact label="Pool" value={deployment.spec.pool || "Not reported"} />
        <Fact label="Region" value={resources.region || "Automatic"} />
        {resources.availability_zone ? (
          <Fact label="Availability zone" value={resources.availability_zone} />
        ) : null}
      </ConfigurationGroup>

      <ConfigurationGroup title="Execution">
        {executesTasks ? (
          <Fact label="Concurrency" value={Intl.NumberFormat().format(resources.concurrency)} />
        ) : null}
        {executesTasks ? (
          <Fact label="Timeout" value={timeoutLabel(resources.timeout_seconds)} />
        ) : null}
        <Fact label="Keep warm" value={retentionLabel(resources.keep_warm)} />
      </ConfigurationGroup>
    </div>
  );
}

function ConfigurationGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <h3 className="mb-3 text-xs font-medium text-foreground">{title}</h3>
      <FactGrid columns={3}>{children}</FactGrid>
    </section>
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

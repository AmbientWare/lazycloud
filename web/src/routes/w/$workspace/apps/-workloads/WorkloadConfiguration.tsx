import type { ReactNode } from "react";

import type { Schemas } from "@/lib/api/client";
import { formatDuration, resourceAllocation } from "@/lib/format";

export function WorkloadConfiguration({ spec }: { spec: Schemas["WorkloadSpec"] }) {
  const { resources, placement } = spec;
  const gpu = resources.gpu ?? [];
  const gpuCount = resources.gpu_count ?? 0;
  // A Pod holds connections rather than executing tasks, so per-task
  // concurrency and timeout describe nothing it does.
  const executesTasks = spec.kind !== "pod";

  return (
    <div className="@container min-w-0 space-y-5 p-4">
      <div className="grid min-w-0 gap-x-8 gap-y-5 @xl:grid-cols-2">
        <ConfigurationGroup title="Runtime">
          <ConfigurationFact label="CPU" value={resourceAllocation(cpuRequest(resources), "CPU")} />
          <ConfigurationFact label="Memory" value={resourceAllocation(memoryRequest(resources))} />
          {gpu.length > 0 ? (
            <ConfigurationFact
              label="GPU"
              value={`${gpu.join(" → ")}${gpuCount > 1 ? ` x${gpuCount}` : ""}`}
            />
          ) : null}
          {placement?.machine ? (
            <ConfigurationFact label="Machine" value={placement.machine} />
          ) : null}
          <ConfigurationFact label="Region" value={placement?.region || "Automatic"} />
          {placement?.availability_zone ? (
            <ConfigurationFact label="Availability zone" value={placement.availability_zone} />
          ) : null}
        </ConfigurationGroup>

        <ConfigurationGroup title="Execution">
          {executesTasks ? (
            <ConfigurationFact
              label="Concurrency"
              value={Intl.NumberFormat().format(spec.concurrency ?? 1)}
            />
          ) : null}
          {executesTasks ? (
            <ConfigurationFact label="Timeout" value={timeoutLabel(spec.timeout_seconds ?? 3600)} />
          ) : null}
          <ConfigurationFact label="Keep warm" value={retentionLabel(spec.keep_warm_seconds)} />
        </ConfigurationGroup>
      </div>
    </div>
  );
}

/** CPUs requested, with the ceiling when one is set. */
export function cpuRequest(resources: Schemas["Resources"]): number | [number, number] {
  const cpu = resources.cpu_millis / 1000;
  return resources.cpu_limit_millis ? [cpu, resources.cpu_limit_millis / 1000] : cpu;
}

/** MiB requested, with the ceiling when one is set. */
export function memoryRequest(resources: Schemas["Resources"]): number | [number, number] {
  const memory = resources.memory_mib;
  return resources.memory_limit_mib ? [memory, resources.memory_limit_mib] : memory;
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

import type { ReactNode } from "react";

import type { Schemas } from "@/lib/api/client";
import { cpuAllocation, formatDuration, memoryAllocation } from "@/lib/format";

export function WorkloadConfiguration({ spec }: { spec: Schemas["FunctionSpec"] }) {
  const resources = spec.resources;
  const autoscaler = spec.autoscaler;
  const retry = spec.retry_policy;

  return (
    <div className="@container min-w-0 space-y-5 p-4">
      <div className="grid min-w-0 gap-x-8 gap-y-5 @xl:grid-cols-2">
        <ConfigurationGroup title="Runtime">
          <ConfigurationFact
            label="CPU"
            value={cpuAllocation(resources.cpu_millis, resources.cpu_limit_millis)}
          />
          <ConfigurationFact
            label="Memory"
            value={memoryAllocation(resources.memory_mib, resources.memory_limit_mib)}
          />
          <ConfigurationFact label="Python" value={spec.image.python_version} />
          <ConfigurationFact
            label="Image"
            value={<span className="mono">{spec.image.image_id ?? "Platform image"}</span>}
          />
          <ConfigurationFact label="Handler" value={<span className="mono">{spec.handler}</span>} />
        </ConfigurationGroup>

        <ConfigurationGroup title="Execution">
          <ConfigurationFact
            label="Concurrency"
            value={Intl.NumberFormat().format(spec.concurrency ?? 1)}
          />
          <ConfigurationFact label="Timeout" value={timeoutLabel(spec.timeout_seconds)} />
          <ConfigurationFact label="Keep warm" value={retentionLabel(spec.keep_warm_seconds)} />
          <ConfigurationFact
            label="Containers"
            value={`${autoscaler?.min_containers ?? 0} to ${autoscaler?.max_containers ?? 1}`}
          />
          <ConfigurationFact
            label="Pending limit"
            value={Intl.NumberFormat().format(spec.max_pending_tasks ?? 100)}
          />
          <ConfigurationFact label="Retries" value={retryLabel(retry)} />
          {spec.secrets?.length ? (
            <ConfigurationFact
              label="Secrets"
              value={<span className="mono">{spec.secrets.join(", ")}</span>}
            />
          ) : null}
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
function retentionLabel(seconds: number | undefined): string {
  if (seconds === undefined) return "Default";
  if (seconds < 0) return "Always warm";
  if (seconds === 0) return "Scale to zero";
  return formatDuration(seconds * 1_000);
}

function timeoutLabel(seconds: number | undefined): string {
  return formatDuration((seconds ?? 3600) * 1_000);
}

function retryLabel(retry: Schemas["RetryPolicy"] | undefined): string {
  if (!retry || retry.max_attempts <= 1) return "None";
  const delay = retry.delay_seconds ? ` · ${formatDuration(retry.delay_seconds * 1_000)}` : "";
  return `${retry.max_attempts - 1} (${retry.backoff ?? "fixed"}${delay})`;
}

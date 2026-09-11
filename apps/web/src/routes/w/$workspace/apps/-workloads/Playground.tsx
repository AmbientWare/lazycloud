import { useMemo, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Loader2, Play } from "lucide-react";

import { PanelError } from "@/components/shared/PanelError";
import { ResultBody } from "@/components/shared/TaskDrawer/ResultBody";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { invokeDeployment, type InvokeResult } from "@/lib/api/invoke";
import type { DeploymentManifest, JsonValue } from "@/lib/api/schemas";
import { deploymentManifestQueryOptions } from "@/lib/queries/apps";
import { taskQueryOptions } from "@/lib/queries/tasks";

import { buildBody, exampleBody, playgroundFields, type PlaygroundField } from "./playground-form";

/**
 * In-UI invoke for a deployed function or endpoint. The form is built
 * from the deployment's recorded client contract (the same schema source
 * `lazycloud client get` uses); flat primitive schemas get typed inputs, anything
 * richer gets a raw JSON editor. Invoke fires the real invoke URL with the
 * session bearer token.
 */
export function Playground({
  workspaceId,
  workspaceName,
  appId,
  workloadName,
  deploymentId,
}: {
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloadName: string;
  deploymentId: string;
}) {
  const manifest = useQuery(deploymentManifestQueryOptions(workspaceId, deploymentId));

  if (manifest.isPending) {
    return (
      <div className="space-y-3 p-4" aria-hidden="true">
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }
  if (manifest.isError) {
    return <PanelError message={manifest.error.message} />;
  }
  return (
    <PlaygroundForm
      manifest={manifest.data}
      workspaceId={workspaceId}
      workspaceName={workspaceName}
      appId={appId}
      workloadName={workloadName}
    />
  );
}

function PlaygroundForm({
  manifest,
  workspaceId,
  workspaceName,
  appId,
  workloadName,
}: {
  manifest: DeploymentManifest;
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloadName: string;
}) {
  const fields = useMemo(() => playgroundFields(manifest), [manifest]);
  const seeded = useMemo(() => JSON.stringify(exampleBody(manifest), null, 2), [manifest]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [rawText, setRawText] = useState(seeded);
  const [inputError, setInputError] = useState<string | null>(null);

  const invoke = useMutation({
    // Same-origin: the published hostname is a different origin to the dashboard,
    // and a deployed resource owes the dashboard no CORS permission.
    mutationFn: (body: JsonValue) => invokeDeployment(manifest.invoke_path, body),
  });

  type BodyResult = { ok: true; body: JsonValue } | { ok: false; message: string };
  const currentBody = (): BodyResult => {
    if (fields !== null && fields.length > 0) {
      const built = buildBody(fields, values);
      if (built.error !== undefined) return { ok: false, message: built.error };
      return { ok: true, body: built.body };
    }
    if (fields !== null && fields.length === 0) return { ok: true, body: {} };
    try {
      return { ok: true, body: JSON.parse(rawText) as JsonValue };
    } catch {
      return { ok: false, message: "payload must be valid JSON" };
    }
  };

  const submit = () => {
    const parsed = currentBody();
    if (!parsed.ok) {
      setInputError(parsed.message);
      return;
    }
    setInputError(null);
    invoke.mutate(parsed.body);
  };

  return (
    <div className="min-w-0 space-y-4 p-4">
      <div className="min-w-0 space-y-3">
        {fields !== null && fields.length > 0 ? (
          <div className="space-y-2.5">
            {fields.map((field) => (
              <FieldInput
                key={field.name}
                field={field}
                value={values[field.name] ?? ""}
                onChange={(next) => setValues((prev) => ({ ...prev, [field.name]: next }))}
              />
            ))}
          </div>
        ) : fields !== null ? (
          <p className="text-sm text-muted-foreground">This workload takes no arguments.</p>
        ) : (
          <div>
            <div className="micro-label mb-1">JSON payload</div>
            <textarea
              value={rawText}
              onChange={(event) => setRawText(event.target.value)}
              spellCheck={false}
              aria-label="JSON payload"
              className="mono h-24 w-full resize-none rounded-md border border-border bg-muted/40 px-2.5 py-2 text-xs outline-none focus:border-ring"
            />
          </div>
        )}
        <div className="flex items-center gap-3">
          <Button size="sm" onClick={submit} disabled={invoke.isPending}>
            {invoke.isPending ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Play className="size-3.5" />
            )}
            Invoke
          </Button>
          {inputError ? <span className="text-xs text-destructive">{inputError}</span> : null}
        </div>
        <InvokeOutcome
          result={invoke.data}
          error={invoke.isError ? invoke.error : null}
          workspaceId={workspaceId}
          workspaceName={workspaceName}
          appId={appId}
          workloadName={workloadName}
        />
      </div>
    </div>
  );
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: PlaygroundField;
  value: string;
  onChange: (value: string) => void;
}) {
  const inputId = `playground-${field.name}`;
  return (
    <div className="flex items-center gap-3">
      <label htmlFor={inputId} className="mono w-32 shrink-0 truncate text-xs" title={field.name}>
        {field.name}
        {field.required ? null : <span className="text-muted-foreground">?</span>}
      </label>
      {field.type === "boolean" ? (
        <select
          id={inputId}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="h-8 rounded-md border border-border bg-muted/40 px-2 text-xs outline-none focus:border-ring"
        >
          <option value="">{field.required ? "select…" : "omit"}</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      ) : (
        <input
          id={inputId}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={field.defaultText || field.type}
          inputMode={field.type === "string" ? "text" : "decimal"}
          className="mono h-8 min-w-0 flex-1 rounded-md border border-border bg-muted/40 px-2.5 text-xs outline-none placeholder:text-muted-foreground/60 focus:border-ring"
        />
      )}
      <span className="w-14 shrink-0 text-right text-[11px] text-muted-foreground">
        {field.type}
      </span>
    </div>
  );
}

function InvokeOutcome({
  result,
  error,
  workspaceId,
  workspaceName,
  appId,
  workloadName,
}: {
  result: InvokeResult | undefined;
  error: Error | null;
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloadName: string;
}) {
  if (error) {
    return <div className="text-xs text-destructive">{error.message}</div>;
  }
  if (!result) return null;

  const meta = (
    <span className="text-[11px] text-muted-foreground">
      HTTP {result.status} · {Math.round(result.durationMs)}ms
    </span>
  );

  if (result.ok && result.taskId) {
    return (
      <TaskInvokeOutcome
        taskId={result.taskId}
        meta={meta}
        workspaceId={workspaceId}
        workspaceName={workspaceName}
        appId={appId}
        workloadName={workloadName}
      />
    );
  }

  return <DirectInvokeOutcome result={result} meta={meta} />;
}

function DirectInvokeOutcome({ result, meta }: { result: InvokeResult; meta: ReactNode }) {
  const response = result.json !== undefined ? result.json : result.bodyText || null;
  return (
    <section className="overflow-hidden rounded-md border border-border bg-muted/20">
      <div className="flex min-h-10 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        {meta}
        <StatusChip status={result.ok ? "complete" : "failed"} />
      </div>
      <ResultBody
        error={result.ok ? null : result.bodyText || "Request failed"}
        result={result.ok ? response : null}
      />
    </section>
  );
}

function TaskInvokeOutcome({
  taskId,
  meta,
  workspaceId,
  workspaceName,
  appId,
  workloadName,
}: {
  taskId: string;
  meta: ReactNode;
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloadName: string;
}) {
  const task = useQuery(taskQueryOptions(workspaceId, taskId));

  return (
    <section className="overflow-hidden rounded-md border border-border bg-muted/20">
      <div className="flex min-h-10 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        {meta}
        {task.data ? (
          <StatusChip status={task.data.status} live={task.data.status === "running"} />
        ) : null}
        <Link
          to="/w/$workspace/apps/$appId/workloads/$name/tasks/$taskId"
          params={{ workspace: workspaceName, appId, name: workloadName, taskId }}
          className="ml-auto flex items-center gap-1 text-xs font-medium text-brand hover:underline"
        >
          Open task
          <ArrowUpRight className="size-3" aria-hidden="true" />
        </Link>
      </div>
      {task.isPending ? (
        <div className="space-y-2 p-3" aria-label="Loading task result">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : task.isError ? (
        <PanelError message={task.error.message} />
      ) : task.data.error || (task.data.result !== null && task.data.result !== undefined) ? (
        <ResultBody error={task.data.error} result={task.data.result} />
      ) : (
        <p className="p-3 text-xs text-muted-foreground">
          {task.data.status === "complete" ? "The task returned no result." : "Result pending."}
        </p>
      )}
    </section>
  );
}

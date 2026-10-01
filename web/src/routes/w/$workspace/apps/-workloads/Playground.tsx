import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Loader2, Play } from "lucide-react";

import { ContentTransition } from "@/components/shared/ContentTransition";
import { PanelError } from "@/components/shared/PanelError";
import { StatusChip } from "@/components/shared/StatusChip";
import { ResultBody } from "@/components/shared/TaskDrawer/ResultBody";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { JsonValue } from "@/lib/api/schemas/json";
import { submitJsonTask, taskQueryOptions, taskResultQueryOptions } from "@/lib/queries/tasks";

import {
  buildBody,
  exampleBody,
  playgroundFields,
  pythonOnlyReason,
  type DeploymentManifest,
  type PlaygroundField,
} from "./playground-form";

/**
 * In-UI invoke for a deployed function. The form is built from the callable
 * contract the deploy recorded; flat primitive parameters get typed inputs,
 * anything richer gets a raw JSON editor. Invoke admits a task with JSON
 * arguments against the active release, as an HTTP call would.
 */
export function Playground({
  workspace,
  resource,
}: {
  workspace: string;
  resource: DeploymentManifest;
}) {
  const pythonRequired = pythonOnlyReason(resource);
  if (pythonRequired) {
    return <p className="p-4 text-sm text-muted-foreground">{pythonRequired}</p>;
  }
  return <PlaygroundForm manifest={resource} workspace={workspace} />;
}

function PlaygroundForm({
  manifest,
  workspace,
}: {
  manifest: DeploymentManifest;
  workspace: string;
}) {
  const fields = useMemo(() => playgroundFields(manifest), [manifest]);
  const seeded = useMemo(() => JSON.stringify(exampleBody(manifest), null, 2), [manifest]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [rawText, setRawText] = useState(seeded);
  const [inputError, setInputError] = useState<string | null>(null);

  const invoke = useMutation({
    mutationFn: (body: JsonValue) =>
      submitJsonTask(workspace, manifest.app, manifest.name, invocationArguments(body)),
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
    <div className="content-transition min-w-0 space-y-4 p-4">
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
        {invoke.isError ? (
          <div className="text-xs text-destructive">{invoke.error.message}</div>
        ) : invoke.data ? (
          <TaskInvokeOutcome taskId={invoke.data.id} manifest={manifest} workspace={workspace} />
        ) : null}
      </div>
    </div>
  );
}

/**
 * The arguments a JSON body stands for: its `args` and `kwargs` when it names
 * them, otherwise the body is the keyword arguments, as with an HTTP invoke.
 */
function invocationArguments(body: JsonValue): {
  args: JsonValue[];
  kwargs: Record<string, JsonValue>;
} {
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    return { args: [], kwargs: {} };
  }
  const { args, kwargs, ...rest } = body;
  if (args === undefined && kwargs === undefined) return { args: [], kwargs: rest };
  return {
    args: Array.isArray(args) ? args : [],
    kwargs: kwargs !== null && typeof kwargs === "object" && !Array.isArray(kwargs) ? kwargs : {},
  };
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

function TaskInvokeOutcome({
  taskId,
  manifest,
  workspace,
}: {
  taskId: string;
  manifest: DeploymentManifest;
  workspace: string;
}) {
  const task = useQuery(taskQueryOptions(workspace, taskId));
  const result = useQuery(
    taskResultQueryOptions(workspace, taskId, task.data?.status === "succeeded"),
  );
  const finished =
    task.data &&
    (task.data.failure || task.data.status === "cancelled" || task.data.status === "succeeded");

  return (
    <section className="overflow-hidden rounded-md border border-border bg-muted/20">
      <div className="flex min-h-10 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <span className="mono text-[11px] text-muted-foreground">{taskId.slice(0, 8)}</span>
        {task.data ? (
          <StatusChip status={task.data.status} live={task.data.status === "running"} />
        ) : null}
        <Link
          to="/w/$workspace/apps/$app/workloads/$kind/$name/tasks/$taskId"
          params={{
            workspace,
            app: manifest.app,
            kind: manifest.kind,
            name: manifest.name,
            taskId,
          }}
          className="ml-auto flex items-center gap-1 text-xs font-medium text-brand hover:underline"
        >
          Open task
          <ArrowUpRight className="size-3" aria-hidden="true" />
        </Link>
      </div>
      <ContentTransition pending={task.isPending}>
        {task.isPending || (task.data?.status === "succeeded" && result.isPending) ? (
          <div className="space-y-2 p-3" aria-label="Loading task result">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : task.isError ? (
          <PanelError message={task.error.message} />
        ) : result.isError ? (
          <PanelError message={result.error.message} />
        ) : finished ? (
          <ResultBody failure={task.data.failure} payload={result.data} />
        ) : (
          <p className="p-3 text-xs text-muted-foreground">Result pending.</p>
        )}
      </ContentTransition>
    </section>
  );
}

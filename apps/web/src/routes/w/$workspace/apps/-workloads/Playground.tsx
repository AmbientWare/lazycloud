import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Loader2, Play } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { invokeDeployment, type InvokeResult } from "@/lib/api/invoke";
import type { DeploymentManifest, JsonValue } from "@/lib/api/schemas";
import { deploymentManifestQueryOptions } from "@/lib/queries/apps";

import {
  buildBody,
  curlSnippet,
  exampleBody,
  playgroundFields,
  pythonSnippet,
  type PlaygroundField,
} from "./playground-form";

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
      workspaceName={workspaceName}
      appId={appId}
      workloadName={workloadName}
    />
  );
}

function PlaygroundForm({
  manifest,
  workspaceName,
  appId,
  workloadName,
}: {
  manifest: DeploymentManifest;
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

  // Snippets mirror what invoke will actually send right now; fall back to the
  // seeded example while the typed form is invalid or empty.
  const snippetParsed = currentBody();
  const snippetBody: JsonValue = snippetParsed.ok ? snippetParsed.body : exampleBody(manifest);

  return (
    <Tabs defaultValue="request" className="flex h-full min-h-0 flex-col">
      <LinearTabsList ariaLabel="Playground views" className="px-4">
        <LinearTab value="request">Request</LinearTab>
        <LinearTab value="curl">curl</LinearTab>
        <LinearTab value="python">Python</LinearTab>
      </LinearTabsList>

      <TabsContent value="request" className="m-0 min-h-0 flex-1 overflow-auto p-4">
        <div className="space-y-3">
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
            <p className="text-sm text-muted-foreground">This target takes no arguments.</p>
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
            kind={manifest.kind}
            result={invoke.data}
            error={invoke.isError ? invoke.error : null}
            workspaceName={workspaceName}
            appId={appId}
            workloadName={workloadName}
          />
        </div>
      </TabsContent>

      <TabsContent value="curl" className="m-0 min-h-0 flex-1 overflow-auto p-4">
        <Snippet text={curlSnippet(manifest.invoke_url, snippetBody)} label="curl snippet" />
      </TabsContent>
      <TabsContent value="python" className="m-0 min-h-0 flex-1 overflow-auto p-4">
        <Snippet text={pythonSnippet(manifest.invoke_url, snippetBody)} label="Python snippet" />
      </TabsContent>
    </Tabs>
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
  kind,
  result,
  error,
  workspaceName,
  appId,
  workloadName,
}: {
  kind: string;
  result: InvokeResult | undefined;
  error: Error | null;
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

  if (!result.ok) {
    return (
      <div className="space-y-1">
        {meta}
        <pre className="mono max-h-48 overflow-auto rounded-md border border-destructive/40 bg-destructive/10 p-2.5 text-xs whitespace-pre-wrap text-destructive">
          {result.bodyText || "request failed"}
        </pre>
      </div>
    );
  }

  // A function invoke creates a task; link it.
  if (result.taskId && kind !== "endpoint") {
    return (
      <div className="flex flex-wrap items-center gap-2">
        {meta}
        <Link
          to="/w/$workspace/apps/$appId/workloads/$name/tasks/$taskId"
          params={{
            workspace: workspaceName,
            appId,
            name: workloadName,
            taskId: result.taskId,
          }}
          className="text-xs text-brand hover:underline"
        >
          View task
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      {meta}
      <pre className="mono max-h-48 overflow-auto rounded-md border border-border bg-muted/40 p-2.5 text-xs whitespace-pre-wrap">
        {prettyBody(result)}
      </pre>
    </div>
  );
}

function prettyBody(result: InvokeResult): string {
  if (result.json !== undefined) return JSON.stringify(result.json, null, 2);
  return result.bodyText || "(empty response)";
}

function Snippet({ text, label }: { text: string; label: string }) {
  return (
    <div className="relative">
      <pre className="mono max-h-40 overflow-auto rounded-md border border-border bg-muted/40 p-2.5 pr-9 text-xs">
        {text}
      </pre>
      <CopyButton value={text} label={label} className="absolute top-1.5 right-1.5 size-7" />
    </div>
  );
}

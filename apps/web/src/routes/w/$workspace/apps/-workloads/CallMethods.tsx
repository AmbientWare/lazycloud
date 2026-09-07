import { useQuery } from "@tanstack/react-query";

import { CopyButton } from "@/components/shared/CopyButton";
import { PanelError } from "@/components/shared/PanelError";
import { highlight, type CodeLanguage } from "@/components/ui/code-syntax";
import { Skeleton } from "@/components/ui/skeleton";
import type { ClientParameter, DeploymentManifest, JsonValue } from "@/lib/api/schemas";
import { deploymentManifestQueryOptions } from "@/lib/queries/apps";

import { curlSnippet, exampleBody, pythonSnippet } from "./playground-form";

export function CallMethods({
  workspaceId,
  deploymentId,
}: {
  workspaceId: string;
  deploymentId: string;
}) {
  const manifest = useQuery(deploymentManifestQueryOptions(workspaceId, deploymentId));

  if (manifest.isPending) {
    return (
      <div className="divide-y divide-border/70" aria-hidden="true">
        <SnippetSkeleton />
        <SnippetSkeleton />
      </div>
    );
  }
  if (manifest.isError) return <PanelError message={manifest.error.message} />;

  const body = exampleBody(manifest.data);
  const generatedClient = typedClientSnippet(manifest.data);

  return (
    <div className="min-w-0 divide-y divide-border/70">
      <Snippet
        title="curl"
        description="Reads LAZYCLOUD_TOKEN from the environment."
        text={curlSnippet(manifest.data.invoke_url, body)}
        label="curl command"
        language="shell"
      />
      <Snippet
        title="Python requests"
        description="Makes the same HTTP request with the requests package."
        text={pythonSnippet(manifest.data.invoke_url, body)}
        label="Python request"
        language="python"
      />
      {generatedClient ? (
        <Snippet
          title="Typed Python client"
          description="Installs the pinned client, then calls the typed method."
          text={generatedClient}
          label="typed Python client"
          language="python"
        />
      ) : null}
    </div>
  );
}

function Snippet({
  title,
  description,
  text,
  label,
  language,
}: {
  title: string;
  description: string;
  text: string;
  label: string;
  language: Exclude<CodeLanguage, "auto">;
}) {
  return (
    <section className="min-w-0 px-4 py-5">
      <div className="min-w-0">
        <h3 className="text-sm font-medium text-foreground">{title}</h3>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p>
      </div>
      <div className="dark mt-3 min-w-0 overflow-hidden border-l-2 border-brand/60 bg-card">
        <div className="flex h-9 items-center justify-between border-b border-border px-3">
          <span className="font-mono text-[10px] font-medium tracking-[0.08em] text-muted-foreground uppercase">
            {language === "shell" ? "Shell" : "Python"}
          </span>
          <CopyButton value={text} label={label} className="size-7" />
        </div>
        <pre
          className="m-0 min-w-0 overflow-x-hidden p-4 font-mono text-xs leading-5 whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]"
          aria-label={label}
          tabIndex={0}
        >
          <code>{highlight(text, language)}</code>
        </pre>
      </div>
    </section>
  );
}

function SnippetSkeleton() {
  return (
    <div className="min-w-0 space-y-3 px-4 py-5">
      <div className="space-y-2 pt-0.5">
        <Skeleton className="h-3 w-20" />
        <Skeleton className="h-3 w-28 max-w-full" />
      </div>
      <Skeleton className="h-40 min-w-0 w-full rounded-none border-l-2 border-brand/20" />
    </div>
  );
}

function typedClientSnippet(manifest: DeploymentManifest): string | null {
  if ((manifest.kind !== "endpoint" && manifest.kind !== "asgi") || !manifest.client_contract) {
    return null;
  }
  const { operation } = manifest.client_contract;
  if (
    operation.parameters.some((parameter) =>
      ["var_positional", "var_keyword"].includes(parameter.parameter_kind),
    )
  ) {
    return null;
  }
  const resource = pythonName(manifest.name);
  const method = pythonName(operation.name);
  const argumentsList = operation.parameters
    .filter((parameter) => parameter.required)
    .map((parameter) => `${pythonName(parameter.name)}=${parameterExample(parameter)}`)
    .join(", ");
  return [
    `lazycloud client get ${manifest.app}`,
    "",
    `from lazycloud_clients import ${manifest.app}`,
    "",
    `result = ${manifest.app}.${resource}.${method}(${argumentsList})`,
  ].join("\n");
}

function parameterExample(parameter: ClientParameter): string {
  if (parameter.default !== null && parameter.default !== undefined) {
    return pythonLiteral(parameter.default);
  }
  const type = parameter.json_schema.type;
  const placeholder: JsonValue =
    type === "string"
      ? "value"
      : type === "integer" || type === "number"
        ? 0
        : type === "boolean"
          ? false
          : type === "array"
            ? []
            : {};
  return pythonLiteral(placeholder);
}

function pythonLiteral(value: JsonValue): string {
  if (value === null) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number" || typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(pythonLiteral).join(", ")}]`;
  return `{${Object.entries(value)
    .map(([key, item]) => `${JSON.stringify(key)}: ${pythonLiteral(item)}`)
    .join(", ")}}`;
}

function pythonName(value: string): string {
  const normalized = value
    .trim()
    .toLowerCase()
    .replaceAll(/\W+/g, "_")
    .replaceAll(/^_+|_+$/g, "");
  if (!normalized || /^\d/.test(normalized)) return `resource_${normalized || "handle"}`;
  return normalized;
}

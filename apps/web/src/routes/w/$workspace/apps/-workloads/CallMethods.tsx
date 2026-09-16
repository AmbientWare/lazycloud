import { useQuery } from "@tanstack/react-query";

import { CopyButton } from "@/components/shared/CopyButton";
import { PanelError } from "@/components/shared/PanelError";
import { highlight, type CodeLanguage } from "@/components/ui/code-syntax";
import { Skeleton } from "@/components/ui/skeleton";
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

  return (
    <div className="content-transition min-w-0 divide-y divide-border/70">
      <p className="px-4 py-3 text-xs text-muted-foreground">
        Set LAZYCLOUD_TOKEN to your access token before running these examples.
      </p>
      <Snippet
        title="curl"
        text={curlSnippet(manifest.data.invoke_url, body)}
        label="curl command"
        language="shell"
      />
      <Snippet
        title="Python requests"
        text={pythonSnippet(manifest.data.invoke_url, body)}
        label="Python request"
        language="python"
      />
    </div>
  );
}

function Snippet({
  title,
  text,
  label,
  language,
}: {
  title: string;
  text: string;
  label: string;
  language: Exclude<CodeLanguage, "auto">;
}) {
  return (
    <section className="min-w-0 px-4 py-5">
      <div className="dark mt-3 min-w-0 overflow-hidden border-l-2 border-brand/60 bg-card">
        <div className="flex h-9 items-center justify-between border-b border-border px-3">
          <h3 className="text-xs font-medium text-foreground">{title}</h3>
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
      <Skeleton className="h-40 min-w-0 w-full rounded-none border-l-2 border-brand/20" />
    </div>
  );
}

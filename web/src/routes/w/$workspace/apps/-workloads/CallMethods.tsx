import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { highlight, type CodeLanguage } from "@/components/ui/code-syntax";
import {
  curlSnippet,
  exampleBody,
  pythonOnlyReason,
  pythonSnippet,
  shellSingleQuote,
  type DeploymentManifest,
} from "./playground-form";
import { pythonCall, sourceImport, sourceSnippet } from "./call-snippets";

export function CallMethods({
  workspaceName,
  resource,
}: {
  workspaceName: string;
  resource: DeploymentManifest;
}) {
  const asgi = resource.kind === "asgi";
  const body = asgi ? undefined : exampleBody(resource);
  const method = "POST";
  const source = sourceImport(resource.handler);
  const pythonRequired = pythonOnlyReason(resource);
  const url = resource.invoke_url;

  return (
    <div className="content-transition min-w-0 divide-y divide-border/70 px-4 py-1">
      {pythonRequired ? (
        <p className="py-3 text-sm text-muted-foreground">{pythonRequired}</p>
      ) : (
        <CallSection title="Typed Python package">
          <Code
            text={`lazycloud app export ${shellSingleQuote(resource.app)} --workspace ${shellSingleQuote(workspaceName)}`}
            language="shell"
            label="Export typed package"
          />
        </CallSection>
      )}
      {source && (
        <CallSection title="Python SDK">
          <Code text={sourceSnippet(resource, source)} language="python" label="SDK calls" />
        </CallSection>
      )}
      {source && !asgi && (
        <CallSection title="Local Python">
          <Code
            text={`${source.importLine}\n\n${pythonCall("result", `${source.reference}.local`, resource)}\nprint(result)`}
            language="python"
            label="Local Python call"
          />
        </CallSection>
      )}
      {!pythonRequired && url && (
        <CallSection title="curl">
          <Code text={curlSnippet(url, body, method)} language="shell" label="curl command" />
        </CallSection>
      )}
      {!pythonRequired && url && (
        <CallSection title="Python requests">
          <Code
            text={pythonSnippet(url, body, method)}
            language="python"
            label="Python HTTP request"
          />
        </CallSection>
      )}
    </div>
  );
}

function CallSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="group min-w-0">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-sm py-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
        <ChevronRight
          aria-hidden="true"
          className="size-3.5 shrink-0 text-muted-foreground transition-transform group-open:rotate-90 motion-reduce:transition-none"
        />
        {title}
      </summary>
      <div className="min-w-0 space-y-3 pb-3">{children}</div>
    </details>
  );
}

function Code({
  text,
  label,
  language,
}: {
  text: string;
  label: string;
  language: Exclude<CodeLanguage, "auto">;
}) {
  return (
    <div className="relative min-w-0 rounded-md border bg-muted/20">
      <CopyButton value={text} label={label} className="absolute top-1.5 right-1.5 size-7" />
      <pre
        className="m-0 overflow-x-auto p-3 pr-10 font-mono text-xs leading-5 text-muted-foreground"
        aria-label={label}
        tabIndex={0}
      >
        <code>{highlight(text, language)}</code>
      </pre>
    </div>
  );
}

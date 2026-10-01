import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { highlight, type CodeLanguage } from "@/components/ui/code-syntax";
import { invokeUrl, type Workload } from "@/lib/queries/deployments";

import {
  clientContract,
  curlSnippet,
  exampleBody,
  pythonOnlyReason,
  pythonSnippet,
  shellSingleQuote,
} from "./playground-form";
import { pythonCall, sourceImport, sourceSnippet } from "./call-snippets";

export function CallMethods({ workspace, workload }: { workspace: string; workload: Workload }) {
  const { deployment, release } = workload;
  const kind = deployment.kind;
  const contract = clientContract(release.spec.client_contract);
  const methods = release.spec.http?.methods ?? [];
  const asgi = kind === "asgi";
  const body = asgi ? undefined : exampleBody(contract);
  const method = asgi && methods.includes("GET") ? "GET" : "POST";
  const source = release.spec.handler ? sourceImport(release.spec.handler) : null;
  const pythonRequired = pythonOnlyReason(contract);
  const url = invokeUrl(workspace, workload);

  return (
    <div className="content-transition min-w-0 divide-y divide-border/70 px-4 py-1">
      {pythonRequired ? (
        <p className="py-3 text-sm text-muted-foreground">{pythonRequired}</p>
      ) : (
        <CallSection title="Typed Python package">
          <Code
            text={`lazycloud app export ${shellSingleQuote(deployment.app)} --workspace ${shellSingleQuote(workspace)}`}
            language="shell"
            label="Export typed package"
          />
        </CallSection>
      )}
      {source && (
        <CallSection title="Python SDK">
          <Code text={sourceSnippet(kind, contract, source)} language="python" label="SDK calls" />
        </CallSection>
      )}
      {source && !asgi && (
        <CallSection title="Local Python">
          <Code
            text={`${source.importLine}\n\n${pythonCall("result", `${source.reference}.local`, contract)}\nprint(result)`}
            language="python"
            label="Local Python call"
          />
        </CallSection>
      )}
      {!pythonRequired && (
        <CallSection title="curl">
          <Code text={curlSnippet(url, body, method)} language="shell" label="curl command" />
        </CallSection>
      )}
      {!pythonRequired && (
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

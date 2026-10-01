import type { ReactNode } from "react";
import { Download, FileCode2 } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import type { Schemas } from "@/lib/api/client";
import { base64ToBytes, downloadBlob } from "@/lib/files";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

const FAILURE_KINDS: Record<Schemas["FailureKind"], string> = {
  user_error: "The function raised an exception",
  load_error: "The handler failed to load",
  timeout: "The task ran past its timeout",
  lost: "The container running it was lost",
  start_failed: "Its container failed to start",
  system: "The platform failed to run it",
  dependency_failed: "A task it depends on did not succeed",
};

/**
 * A task's recorded outcome: the value it returned, or how it failed. A Python
 * value is only ever offered as a file; the dashboard never evaluates it.
 */
export function ResultBody({
  failure,
  payload,
}: {
  failure: Schemas["TaskFailure"] | null | undefined;
  payload: Schemas["Payload"] | null | undefined;
}): ReactNode {
  if (failure) {
    const heading = failure.type ? `${failure.type}: ${failure.message}` : failure.message;
    const text = failure.traceback ? `${failure.traceback.trimEnd()}` : heading;
    return (
      <TextResult
        label="Error"
        detail={FAILURE_KINDS[failure.kind] ?? failure.kind}
        text={text}
        extension="txt"
        tone="error"
      />
    );
  }
  if (!payload) {
    return <PanelEmpty message="No result recorded" className="h-24" />;
  }
  if (payload.encoding === "json") {
    return (
      <TextResult label="Result" text={JSON.stringify(payload.value, null, 2)} extension="json" />
    );
  }
  return <PythonResult data={payload.data ?? ""} />;
}

function PythonResult({ data }: { data: string }) {
  const bytes = base64ToBytes(data);
  return (
    <div className="flex min-h-full flex-col">
      <div className="flex h-10 shrink-0 items-center border-b border-border px-2">
        <span className="px-1 text-xs text-muted-foreground">Result</span>
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            aria-label="Download Python object"
            title={`Download Python object (${formatBytes(bytes.byteLength)}, pickle)`}
            onClick={() =>
              downloadBlob(
                "task-result.pkl",
                new Blob([bytes], { type: "application/octet-stream" }),
              )
            }
          >
            <FileCode2 className="size-3.5" />
          </Button>
        </div>
      </div>
      <PanelEmpty
        message={`Python object, ${formatBytes(bytes.byteLength)}`}
        detail="Download it and load it with the Python SDK."
        className="h-24"
      />
    </div>
  );
}

function TextResult({
  label,
  detail,
  text,
  extension,
  tone = "neutral",
}: {
  label: "Result" | "Error";
  detail?: string;
  text: string;
  extension: "json" | "txt";
  tone?: "neutral" | "error";
}) {
  return (
    <div className="flex min-h-full flex-col">
      <div className="flex h-10 shrink-0 items-center border-b border-border px-2">
        <span className="px-1 text-xs text-muted-foreground">{label}</span>
        {detail ? <span className="px-1 text-xs text-muted-foreground">· {detail}</span> : null}
        <div className="ml-auto flex items-center gap-1">
          <CopyButton value={text} label={label.toLowerCase()} />
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Download ${label.toLowerCase()}`}
            title={`Download ${label.toLowerCase()}`}
            onClick={() =>
              downloadBlob(
                `task-${label.toLowerCase()}.${extension}`,
                new Blob([text], { type: "text/plain;charset=utf-8" }),
              )
            }
          >
            <Download className="size-3.5" />
          </Button>
        </div>
      </div>
      <pre
        className={cn(
          "mono min-h-0 flex-1 whitespace-pre-wrap break-all p-3 text-xs",
          tone === "error" && "text-destructive",
        )}
      >
        {text}
      </pre>
    </div>
  );
}

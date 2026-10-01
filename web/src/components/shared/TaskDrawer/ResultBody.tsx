import type { ReactNode } from "react";
import { Download, FileCode2 } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import type { Schemas } from "@/lib/api/client";
import { base64ToBytes, downloadBlob } from "@/lib/files";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

/** A task failure as the text a person reads: the traceback, else the exception. */
export function failureText(failure: Schemas["TaskFailure"]): string {
  if (failure.traceback) return failure.traceback.trimEnd();
  return failure.type ? `${failure.type}: ${failure.message}` : failure.message;
}

/**
 * A task's recorded outcome. A JSON result shows as text; a Python object is
 * only ever offered as a file.
 */
export function ResultBody({
  error,
  result,
}: {
  error: string | null | undefined;
  result: Schemas["Payload"] | null | undefined;
}): ReactNode {
  if (error) {
    return <TextResult label="Error" text={error} extension="txt" tone="error" />;
  }
  if (!result) {
    return <PanelEmpty message="No result recorded" className="h-24" />;
  }
  if (result.encoding === "json") {
    return (
      <TextResult
        label="Result"
        text={JSON.stringify(result.value ?? null, null, 2)}
        extension="json"
      />
    );
  }
  return <PythonResult data={result.data ?? ""} />;
}

function PythonResult({ data }: { data: string }) {
  const bytes = base64ToBytes(data);
  const downloadPickle = () =>
    downloadBlob("task-result.pkl", new Blob([bytes], { type: "application/octet-stream" }));

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
            onClick={downloadPickle}
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
  text,
  extension,
  tone = "neutral",
}: {
  label: "Result" | "Error";
  text: string;
  extension: "json" | "txt";
  tone?: "neutral" | "error";
}) {
  return (
    <div className="flex min-h-full flex-col">
      <div className="flex h-10 shrink-0 items-center border-b border-border px-2">
        <span className="px-1 text-xs text-muted-foreground">{label}</span>
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

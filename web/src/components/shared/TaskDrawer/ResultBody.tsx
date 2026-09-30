import { useState, type ReactNode } from "react";
import { Download, FileCode2 } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import {
  functionResultSchema,
  type FunctionCloudpickleResult,
  type FunctionResultRichDisplay,
} from "@/lib/api/schemas/functions";
import { base64ToBytes, downloadBlob } from "@/lib/files";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * A task's recorded outcome. A Python result shows the display the runner
 * stored beside the pickle; the pickle itself is only ever offered as a file.
 */
export function ResultBody({
  error,
  result,
}: {
  error: string | null | undefined;
  result: unknown;
}): ReactNode {
  if (error) {
    return <TextResult label="Error" text={error} extension="txt" tone="error" />;
  }
  if (result === null || result === undefined) {
    return <PanelEmpty message="No result recorded" className="h-24" />;
  }
  const parsed = functionResultSchema.safeParse(result);
  if (!parsed.success) {
    return <TextResult label="Result" text={JSON.stringify(result, null, 2)} extension="json" />;
  }
  if (parsed.data.encoding === "json") {
    return (
      <TextResult
        label="Result"
        text={JSON.stringify(parsed.data.value, null, 2)}
        extension="json"
      />
    );
  }
  return <PythonResult payload={parsed.data} />;
}

function PythonResult({ payload }: { payload: FunctionCloudpickleResult }) {
  const display = payload.display;
  const rich = display?.rich ?? null;
  const [view, setView] = useState<"rendered" | "text">("rendered");
  const showRich = rich !== null && view === "rendered";

  const downloadPickle = () =>
    downloadBlob(
      "task-result.pkl",
      new Blob([base64ToBytes(payload.value_base64)], { type: "application/octet-stream" }),
    );

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex h-10 shrink-0 items-center border-b border-border px-2">
        <span className="px-1 text-xs text-muted-foreground">Result</span>
        {rich !== null ? (
          <div className="ml-2 flex items-center gap-0.5" role="group" aria-label="Result view">
            <ViewToggle active={view === "rendered"} onClick={() => setView("rendered")}>
              Rendered
            </ViewToggle>
            <ViewToggle active={view === "text"} onClick={() => setView("text")}>
              Text
            </ViewToggle>
          </div>
        ) : null}
        <div className="ml-auto flex items-center gap-1">
          {display ? <CopyButton value={display.text} label="result text" /> : null}
          {rich !== null ? <RichDownload rich={rich} /> : null}
          <Button
            variant="ghost"
            size="icon"
            aria-label="Download Python object"
            title={`Download Python object (${formatBytes(payload.size_bytes)}, pickle)`}
            onClick={downloadPickle}
          >
            <FileCode2 className="size-3.5" />
          </Button>
        </div>
      </div>
      {display === null ? (
        <PanelEmpty
          message={`Python object, ${formatBytes(payload.size_bytes)}`}
          detail="Download it and load it with the Python SDK."
          className="h-24"
        />
      ) : showRich ? (
        <RichDisplay rich={rich} />
      ) : (
        <pre className="mono min-h-0 flex-1 whitespace-pre-wrap break-all p-3 text-xs">
          {display.text}
        </pre>
      )}
    </div>
  );
}

function ViewToggle({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-pressed={active}
      onClick={onClick}
      className={cn("h-6 px-1.5 text-xs", active && "bg-accent text-foreground")}
    >
      {children}
    </Button>
  );
}

function RichDownload({ rich }: { rich: FunctionResultRichDisplay }) {
  const download = () => {
    if (rich.kind === "image") {
      downloadBlob(
        "task-result.png",
        new Blob([base64ToBytes(rich.value_base64)], { type: rich.media_type }),
      );
      return;
    }
    downloadBlob("task-result.html", new Blob([rich.html], { type: "text/html;charset=utf-8" }));
  };
  const what = rich.kind === "image" ? "image" : "HTML";
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={`Download ${what}`}
      title={`Download ${what}`}
      onClick={download}
    >
      <Download className="size-3.5" />
    </Button>
  );
}

function RichDisplay({ rich }: { rich: FunctionResultRichDisplay }) {
  if (rich.kind === "image") {
    return (
      <div className="min-h-0 flex-1 overflow-auto p-3">
        <img
          src={`data:${rich.media_type};base64,${rich.value_base64}`}
          alt="Result image"
          className="max-w-full"
        />
      </div>
    );
  }
  return (
    <iframe
      title="Rendered result"
      sandbox=""
      srcDoc={htmlDocument(rich.html)}
      className="min-h-0 w-full flex-1 border-0 bg-transparent"
    />
  );
}

/**
 * The sandboxed frame cannot read our stylesheet, so the rules a bare table
 * needs on the dark panel travel with the document.
 */
function htmlDocument(html: string): string {
  return (
    '<!doctype html><html><head><meta charset="utf-8"><style>' +
    ":root{color-scheme:dark}" +
    "body{margin:0;padding:12px;background:transparent;color:CanvasText;" +
    "font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}" +
    "table{border-collapse:collapse}" +
    "th,td{border:1px solid color-mix(in srgb,CanvasText 22%,transparent);padding:2px 8px;text-align:right}" +
    "th{font-weight:600}" +
    "img,svg{max-width:100%}" +
    "</style></head><body>" +
    html +
    "</body></html>"
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

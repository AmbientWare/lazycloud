import { useState, type ReactNode } from "react";
import { Download, FileCode2 } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import type { Schemas } from "@/lib/api/client";
import { base64ToBytes, downloadBlob } from "@/lib/files";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

type RichDisplay = Schemas["RichDisplay"];

/**
 * A task's recorded outcome. A Python result shows the display the runner
 * made of the value where it ran; the pickle itself is only ever offered as
 * a file.
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
  return <PythonResult data={result.data ?? ""} display={result.display ?? null} />;
}

function PythonResult({
  data,
  display,
}: {
  data: string;
  display: Schemas["ResultDisplay"] | null;
}) {
  const bytes = base64ToBytes(data);
  const rich = display?.rich ?? null;
  const [view, setView] = useState<"rendered" | "text">("rendered");
  const showRich = rich !== null && view === "rendered";

  const downloadPickle = () =>
    downloadBlob("task-result.pkl", new Blob([bytes], { type: "application/octet-stream" }));

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
            title={`Download Python object (${formatBytes(bytes.byteLength)}, pickle)`}
            onClick={downloadPickle}
          >
            <FileCode2 className="size-3.5" />
          </Button>
        </div>
      </div>
      {display === null ? (
        <PanelEmpty
          message={`Python object, ${formatBytes(bytes.byteLength)}`}
          detail="Download it and load it with the Python SDK."
          className="h-24"
        />
      ) : showRich ? (
        <RichView rich={rich} />
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

function RichDownload({ rich }: { rich: RichDisplay }) {
  const download = () => {
    if (rich.kind === "image") {
      downloadBlob(
        "task-result.png",
        new Blob([base64ToBytes(rich.value_base64 ?? "")], { type: "image/png" }),
      );
      return;
    }
    downloadBlob(
      "task-result.html",
      new Blob([rich.html ?? ""], { type: "text/html;charset=utf-8" }),
    );
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

function RichView({ rich }: { rich: RichDisplay }) {
  if (rich.kind === "image") {
    // Always a PNG data URL: the server keeps no other image.
    return (
      <div className="min-h-0 flex-1 overflow-auto p-3">
        <img
          src={`data:image/png;base64,${rich.value_base64 ?? ""}`}
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
      referrerPolicy="no-referrer"
      srcDoc={htmlDocument(rich.html ?? "")}
      className="min-h-0 w-full flex-1 border-0 bg-transparent"
    />
  );
}

// What inertHtml removes, as the host session does before it keeps a
// display: elements that navigate the frame, load a document or run code,
// and attributes that navigate, load or submit.
const DROPPED_ELEMENTS =
  "script,iframe,frame,frameset,object,applet,noscript,template,form,portal,meta,base,link,embed,set,animate,animateMotion,animateTransform";
const DROPPED_ATTRIBUTES = new Set([
  "href",
  "xlink:href",
  "action",
  "formaction",
  "srcset",
  "ping",
  "http-equiv",
  "srcdoc",
  "background",
  "poster",
  "data",
  "codebase",
  "manifest",
  "target",
]);

/**
 * The HTML with everything that could redirect the frame, load from the
 * network or run code removed. DOMParser builds an inert document: nothing
 * in it runs or loads while it is cleaned.
 */
function inertHtml(html: string): string {
  const doc = new DOMParser().parseFromString(`<!doctype html><body>${html}`, "text/html");
  doc.querySelectorAll(DROPPED_ELEMENTS).forEach((element) => element.remove());
  for (const element of doc.body.querySelectorAll("*")) {
    for (const { name, value } of [...element.attributes]) {
      const key = name.toLowerCase();
      const inlineImage = key === "src" && /^\s*data:image\//i.test(value);
      if (key.startsWith("on") || DROPPED_ATTRIBUTES.has(key) || (key === "src" && !inlineImage)) {
        element.removeAttribute(name);
      }
    }
  }
  return doc.body.innerHTML;
}

/**
 * The HTML a result renders runs in a frame with no scripts, its own opaque
 * origin and a policy that loads nothing from the network, so it can neither
 * act on the dashboard nor reveal who viewed it, and without anything that
 * could navigate the frame. The frame cannot read our stylesheet, so the
 * rules a bare table needs on the dark panel travel with the document.
 */
function htmlDocument(html: string): string {
  return (
    '<!doctype html><html><head><meta charset="utf-8">' +
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; ' +
    "img-src data:; style-src 'unsafe-inline'; font-src data:\">" +
    "<style>" +
    ":root{color-scheme:dark}" +
    "body{margin:0;padding:12px;background:transparent;color:CanvasText;" +
    "font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}" +
    "table{border-collapse:collapse}" +
    "th,td{border:1px solid color-mix(in srgb,CanvasText 22%,transparent);padding:2px 8px;text-align:right}" +
    "th{font-weight:600}" +
    "img,svg{max-width:100%}" +
    "</style></head><body>" +
    inertHtml(html) +
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

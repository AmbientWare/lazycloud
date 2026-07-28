import { useState, type ReactNode } from "react";
import { Check, Copy, Download } from "lucide-react";

import { Button } from "@/components/ui/button";

export function ResultBody({ error, result }: { error: string | null | undefined; result: unknown }): ReactNode {
  const [copied, setCopied] = useState(false);
  const content = error
    ? error
    : result !== null && result !== undefined
      ? JSON.stringify(result, null, 2)
      : null;

  if (content === null) {
    return (
      <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">
        No result recorded
      </div>
    );
  }

  const extension = error ? "txt" : "json";
  return (
    <div className="flex min-h-full flex-col">
      <div className="flex h-10 shrink-0 items-center border-b border-border px-2">
        <span className="px-1 text-xs text-muted-foreground">{error ? "Error" : "Result"}</span>
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Copy ${error ? "error" : "result"}`}
            title={`Copy ${error ? "error" : "result"}`}
            onClick={() => {
              void navigator.clipboard.writeText(content).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1_500);
              });
            }}
          >
            {copied ? <Check className="size-3.5 text-positive" /> : <Copy className="size-3.5" />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Download ${error ? "error" : "result"}`}
            title={`Download ${error ? "error" : "result"}`}
            onClick={() => downloadText(`task-${error ? "error" : "result"}.${extension}`, content)}
          >
            <Download className="size-3.5" />
          </Button>
        </div>
      </div>
      <pre
        className={
          error
            ? "mono min-h-0 flex-1 whitespace-pre-wrap break-all p-3 text-xs text-destructive"
            : "mono min-h-0 flex-1 whitespace-pre-wrap break-all p-3 text-xs"
        }
      >
        {content}
      </pre>
    </div>
  );
}

function downloadText(filename: string, content: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

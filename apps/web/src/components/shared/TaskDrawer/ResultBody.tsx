import type { ReactNode } from "react";
import { Download } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";

export function ResultBody({
  error,
  result,
}: {
  error: string | null | undefined;
  result: unknown;
}): ReactNode {
  const content = error
    ? error
    : result !== null && result !== undefined
      ? JSON.stringify(result, null, 2)
      : null;

  if (content === null) {
    return <PanelEmpty message="No result recorded" className="h-24" />;
  }

  const extension = error ? "txt" : "json";
  return (
    <div className="flex min-h-full flex-col">
      <div className="flex h-10 shrink-0 items-center border-b border-border px-2">
        <span className="px-1 text-xs text-muted-foreground">{error ? "Error" : "Result"}</span>
        <div className="ml-auto flex items-center gap-1">
          <CopyButton value={content} label={error ? "error" : "result"} />
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

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { ContentTransition } from "@/components/shared/ContentTransition";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import type { ArtifactSummary } from "@/lib/api/schemas";
import { artifactContentQueryOptions } from "@/lib/queries/artifacts";

export type PreviewKind = "image" | "pdf" | "text";

export function ArtifactPreview({
  artifact,
  workspaceId,
  kind,
}: {
  artifact: ArtifactSummary;
  workspaceId: string;
  kind: PreviewKind;
}) {
  const query = useQuery(artifactContentQueryOptions(workspaceId, artifact));
  const blob = query.data;
  const [decoded, setDecoded] = useState<{ blob: Blob; url: string; text: string | null } | null>(
    null,
  );
  const [decodeFailure, setDecodeFailure] = useState<Blob | null>(null);
  const [pdfLoaded, setPdfLoaded] = useState(false);
  const content = decoded?.blob === blob ? decoded : null;
  const failure =
    query.error?.message ??
    (decodeFailure && decodeFailure === blob
      ? "This file could not be previewed. Download it to inspect the file."
      : null);
  const pending = !failure && (!content || (kind === "pdf" && !pdfLoaded));

  useEffect(() => {
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    let cancelled = false;
    void (async () => {
      try {
        const text = kind === "text" ? await blob.text() : null;
        if (kind === "image") {
          const image = new Image();
          image.src = url;
          await image.decode();
        }
        if (!cancelled) setDecoded({ blob, url, text });
      } catch {
        if (!cancelled) setDecodeFailure(blob);
      }
    })();
    return () => {
      cancelled = true;
      URL.revokeObjectURL(url);
    };
  }, [blob, kind]);

  return (
    <div
      aria-busy={pending}
      className="relative flex min-h-0 flex-1 flex-col overflow-hidden bg-card"
    >
      {pending && <PreviewLoading />}
      {failure ? (
        <div className="m-auto p-4 text-center">
          <PanelError message={failure} />
          {query.isError && (
            <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
              Retry
            </Button>
          )}
        </div>
      ) : (
        content && (
          <ContentTransition pending={pending} className="flex min-h-0 flex-1 flex-col">
            {kind === "pdf" ? (
              <iframe
                src={content.url}
                title={artifact.filename}
                onLoad={() => setPdfLoaded(true)}
                className={`min-h-0 w-full flex-1 border-0 ${pdfLoaded ? "visible" : "invisible"}`}
              />
            ) : kind === "image" ? (
              <div className="flex min-h-0 flex-1 items-center justify-center p-3">
                <img
                  src={content.url}
                  alt={artifact.filename}
                  className="max-h-full max-w-full object-contain"
                />
              </div>
            ) : (
              <pre className="mono min-h-0 flex-1 overflow-auto p-4 text-xs whitespace-pre-wrap break-words">
                {content.text}
              </pre>
            )}
          </ContentTransition>
        )
      )}
    </div>
  );
}

function PreviewLoading() {
  return (
    <ContentTransition
      pending
      role="status"
      aria-label="Loading preview"
      className="absolute inset-0 flex items-center justify-center"
    >
      <Loader2
        className="size-5 animate-spin text-muted-foreground motion-reduce:animate-none"
        aria-hidden="true"
      />
    </ContentTransition>
  );
}

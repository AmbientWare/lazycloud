import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { ContentTransition } from "@/components/shared/ContentTransition";
import { FilePreviewBody, ImagePreview, TextPreview } from "@/components/shared/FilePreview";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import { artifactPreviewQuery, type Artifact, type PreviewKind } from "@/lib/queries/artifacts";

export function ArtifactPreview({
  artifact,
  workspaceId,
  kind,
}: {
  artifact: Artifact;
  workspaceId: string;
  kind: PreviewKind;
}) {
  const query = useQuery(artifactPreviewQuery(workspaceId, artifact.id, kind));
  const [pdfLoaded, setPdfLoaded] = useState(false);
  const content = query.data;
  const failure = query.isError
    ? "This file could not be previewed. Download it to inspect the file."
    : null;
  const pending = !failure && (!content || (kind === "pdf" && !pdfLoaded));

  return (
    <FilePreviewBody pending={pending}>
      {failure ? (
        <div className="m-auto p-4 text-center">
          <PanelError message={failure} />
          <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
            Retry
          </Button>
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
              <ImagePreview url={content.url} alt={artifact.filename} />
            ) : (
              <TextPreview text={content.text ?? ""} />
            )}
          </ContentTransition>
        )
      )}
    </FilePreviewBody>
  );
}

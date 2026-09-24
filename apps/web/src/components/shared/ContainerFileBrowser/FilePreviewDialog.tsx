import { useEffect, useMemo, useState, type RefObject } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Download, Loader2 } from "lucide-react";

import { ContentTransition } from "@/components/shared/ContentTransition";
import {
  FilePreviewBody,
  FilePreviewDialog,
  ImagePreview,
  TextPreview,
} from "@/components/shared/FilePreview";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import type { PodFileInfo } from "@/lib/api/schemas";
import { base64ToBytes, downloadBlob } from "@/lib/files";
import { formatBytes, relativeTime } from "@/lib/format";
import {
  CONTAINER_FILE_IMAGE_PREVIEW_BYTES,
  CONTAINER_FILE_PREVIEW_BYTES,
  containerFilePreviewQueryOptions,
  downloadContainerFile,
} from "@/lib/queries/container-files";

import {
  describeFileContents,
  formatMode,
  hexDump,
  imageMimeForName,
  type FileContents,
} from "./file-contents";

/** How many leading bytes a file that is not text or an image shows. */
const HEX_PREVIEW_BYTES = 512;

export type PreviewTarget = { path: string; file: PodFileInfo };

/** A container file in the shared preview dialog, drawn as an image, text, or its first bytes. */
export function ContainerFilePreviewDialog({
  workspaceId,
  containerId,
  target,
  open,
  onOpenChange,
  returnFocus,
}: {
  workspaceId: string;
  containerId: string;
  target: PreviewTarget;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  returnFocus: RefObject<HTMLElement | null>;
}) {
  const { path, file } = target;
  const drawable =
    imageMimeForName(file.name) !== undefined && file.size <= CONTAINER_FILE_IMAGE_PREVIEW_BYTES;
  const query = useQuery(
    containerFilePreviewQueryOptions(
      workspaceId,
      containerId,
      path,
      drawable ? CONTAINER_FILE_IMAGE_PREVIEW_BYTES : CONTAINER_FILE_PREVIEW_BYTES,
    ),
  );
  const read = useMemo(() => {
    if (!query.data) return undefined;
    const bytes = base64ToBytes(query.data.value_base64);
    return {
      bytes,
      truncated: query.data.truncated,
      contents: describeFileContents(file.name, bytes, !query.data.truncated),
    };
  }, [query.data, file.name]);
  const image = useDecodedImage(read?.contents, read?.bytes);
  const contents: FileContents | undefined =
    image.failed && read ? { kind: "binary", label: read.contents.label } : read?.contents;
  const download = useMutation({
    mutationFn: async () => {
      const whole = await downloadContainerFile(workspaceId, containerId, path);
      downloadBlob(file.name, new Blob([base64ToBytes(whole.value_base64)]));
    },
  });

  const description = [
    contents?.label,
    formatBytes(file.size),
    formatMode(file.mode),
    file.owner ? `uid ${file.owner} gid ${file.group || file.owner}` : undefined,
    file.mod_time ? `modified ${relativeTime(file.mod_time)}` : undefined,
  ]
    .filter(Boolean)
    .join(" · ");
  const pending = query.isPending || (contents?.kind === "image" && !image.url);

  return (
    <FilePreviewDialog
      open={open}
      onOpenChange={onOpenChange}
      title={path}
      description={description}
      returnFocus={returnFocus}
      actions={
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="shrink-0"
          disabled={download.isPending}
          onClick={() => download.mutate()}
        >
          {download.isPending ? (
            <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : (
            <Download aria-hidden="true" />
          )}
          Download
        </Button>
      }
    >
      {download.isError ? (
        <p role="alert" className="shrink-0 border-b px-4 py-2 text-xs text-destructive">
          {download.error.message}
        </p>
      ) : null}
      <FilePreviewBody pending={pending}>
        {query.isError ? (
          <div className="m-auto p-4">
            <PanelError message={query.error.message} />
          </div>
        ) : contents && read && !pending ? (
          <ContentTransition pending={false} className="flex min-h-0 flex-1 flex-col">
            {contents.kind === "image" && image.url ? (
              <ImagePreview url={image.url} alt={file.name} />
            ) : contents.kind === "text" ? (
              <TextPreview text={contents.text} />
            ) : (
              <BinaryPreview
                bytes={read.bytes}
                oversizedImage={
                  !drawable && imageMimeForName(file.name) !== undefined && read.truncated
                }
              />
            )}
            {contents.kind === "text" && read.truncated ? (
              <p className="shrink-0 border-t px-4 py-2 text-xs text-muted-foreground">
                Showing the first {formatBytes(read.bytes.length)} of {formatBytes(file.size)}.
              </p>
            ) : null}
          </ContentTransition>
        ) : null}
      </FilePreviewBody>
    </FilePreviewDialog>
  );
}

function BinaryPreview({ bytes, oversizedImage }: { bytes: Uint8Array; oversizedImage: boolean }) {
  const shown = bytes.subarray(0, HEX_PREVIEW_BYTES);
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-auto p-4">
      <p className="mb-2 text-xs text-muted-foreground">
        {oversizedImage
          ? `Images over ${formatBytes(CONTAINER_FILE_IMAGE_PREVIEW_BYTES)} are not drawn. First ${formatBytes(shown.length)}:`
          : `First ${formatBytes(shown.length)}:`}
      </p>
      <pre className="mono text-xs leading-5 whitespace-pre text-foreground">{hexDump(shown)}</pre>
    </div>
  );
}

/**
 * An object URL for image contents once the browser has decoded it, or `failed`
 * when it could not, so a mislabeled file falls back to its bytes.
 */
function useDecodedImage(
  contents: FileContents | undefined,
  bytes: Uint8Array<ArrayBuffer> | undefined,
): { url: string | undefined; failed: boolean } {
  const [state, setState] = useState<{
    bytes: Uint8Array;
    url?: string;
    failed: boolean;
  } | null>(null);
  const mime = contents?.kind === "image" ? contents.mime : undefined;

  useEffect(() => {
    if (!mime || !bytes) return;
    const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    let cancelled = false;
    const image = new Image();
    image.src = url;
    image.decode().then(
      () => {
        if (!cancelled) setState({ bytes, url, failed: false });
      },
      () => {
        if (!cancelled) setState({ bytes, failed: true });
      },
    );
    return () => {
      cancelled = true;
      URL.revokeObjectURL(url);
    };
  }, [mime, bytes]);

  const current = state?.bytes === bytes ? state : null;
  return { url: current?.url, failed: current?.failed ?? false };
}

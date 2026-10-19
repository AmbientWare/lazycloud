"use client";

import { ChevronLeft, ChevronRight, Download, Link2, RotateCcw, X } from "lucide-react";
import { type KeyboardEvent, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

import type { GalleryItem, Options, ShareLink } from "./api";
import type { GalleryImage } from "./gallery";

const ON_DARK = "text-plate-foreground hover:bg-white/10 hover:text-plate-foreground";

export function Lightbox({
  images,
  position,
  options,
  onMove,
  onClose,
  onShare,
  onReuse,
}: {
  images: GalleryImage[];
  position: number | null;
  options: Options;
  onMove: (position: number) => void;
  onClose: () => void;
  onShare: (jobId: string, index: number) => Promise<ShareLink>;
  onReuse: (item: GalleryItem) => void;
}) {
  const image = position === null ? undefined : images[position];
  // The tile that opened the lightbox gets focus back when it closes.
  const opener = useRef<HTMLElement | null>(null);

  const step = (by: number) => {
    if (position !== null) onMove((position + by + images.length) % images.length);
  };

  const stepOnArrows = (event: KeyboardEvent) => {
    if (event.target instanceof HTMLInputElement) return;
    if (event.key === "ArrowLeft") step(-1);
    if (event.key === "ArrowRight") step(1);
  };

  return (
    <Dialog open={image !== undefined} onOpenChange={(open) => open || onClose()}>
      {image && position !== null && (
        <DialogContent
          showCloseButton={false}
          onKeyDown={stepOnArrows}
          onOpenAutoFocus={() => {
            opener.current = document.activeElement as HTMLElement | null;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            opener.current?.focus();
          }}
          className="grid h-dvh max-h-dvh w-screen max-w-none content-start gap-0 overflow-y-auto rounded-none bg-plate-deep p-0 text-plate-foreground ring-0 sm:max-w-none lg:grid-cols-[minmax(0,1fr)_25rem] lg:content-stretch lg:overflow-hidden"
        >
          <div className="relative grid place-items-center px-4 pt-16 pb-6 sm:px-16 lg:p-20">
            <img
              key={image.url}
              src={image.url}
              alt={image.item.prompt}
              className="max-h-[60dvh] max-w-full rounded-sm object-contain shadow-2xl duration-300 animate-in fade-in-0 lg:max-h-[calc(100dvh-10rem)]"
            />
            {images.length > 1 && (
              <>
                <Button
                  variant="ghost"
                  size="icon-lg"
                  onClick={() => step(-1)}
                  aria-label="Previous image"
                  className={`absolute top-[calc(50%-1.125rem)] left-2 rounded-full bg-black/40 lg:left-5 ${ON_DARK}`}
                >
                  <ChevronLeft />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-lg"
                  onClick={() => step(1)}
                  aria-label="Next image"
                  className={`absolute top-[calc(50%-1.125rem)] right-2 rounded-full bg-black/40 lg:right-5 ${ON_DARK}`}
                >
                  <ChevronRight />
                </Button>
              </>
            )}
          </div>
          <DialogClose asChild>
            <Button
              variant="ghost"
              size="icon-lg"
              aria-label="Close"
              className={`absolute top-3 right-3 rounded-full ${ON_DARK}`}
            >
              <X />
            </Button>
          </DialogClose>
          <Details
            key={image.url}
            image={image}
            label={`${position + 1} of ${images.length}`}
            options={options}
            onShare={onShare}
            onReuse={onReuse}
          />
        </DialogContent>
      )}
    </Dialog>
  );
}

function Details({
  image,
  label,
  options,
  onShare,
  onReuse,
}: {
  image: GalleryImage;
  label: string;
  options: Options;
  onShare: (jobId: string, index: number) => Promise<ShareLink>;
  onReuse: (item: GalleryItem) => void;
}) {
  const { item, index, url } = image;
  const aspect = options.aspects.find((choice) => choice.id === item.aspect);
  const style = options.styles.find((choice) => choice.id === item.style);
  const [share, setShare] = useState<
    | { state: "idle" | "working" }
    | { state: "ready"; link: ShareLink; copied: boolean }
    | { state: "failed"; message: string }
  >({ state: "idle" });

  const createShareLink = async () => {
    setShare({ state: "working" });
    try {
      const link = await onShare(item.job_id, index);
      const copied = await navigator.clipboard.writeText(link.url).then(
        () => true,
        () => false,
      );
      setShare({ state: "ready", link, copied });
    } catch (error) {
      setShare({
        state: "failed",
        message: error instanceof Error ? error.message : "The share link could not be made.",
      });
    }
  };

  return (
    <aside className="grid content-start gap-7 border-plate-line p-6 max-lg:border-t sm:p-8 lg:overflow-y-auto lg:border-l">
      <DialogTitle className="text-sm font-normal text-plate-muted">Image {label}</DialogTitle>

      <DialogDescription className="font-serif text-2xl leading-snug text-plate-foreground">
        {item.prompt}
      </DialogDescription>

      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
        <dt className="text-plate-muted">Style</dt>
        <dd>{style?.label ?? item.style}</dd>
        <dt className="text-plate-muted">Size</dt>
        <dd>{aspect ? `${aspect.label}, ${aspect.width} × ${aspect.height}` : item.aspect}</dd>
        <dt className="text-plate-muted">Seed</dt>
        <dd className="tabular-nums">{item.seed + index}</dd>
        <dt className="text-plate-muted">Made</dt>
        <dd>
          {new Date(item.created_at).toLocaleString(undefined, {
            dateStyle: "medium",
            timeStyle: "short",
          })}
        </dd>
      </dl>

      <div className="grid gap-2">
        <Button asChild className="h-10 rounded-full">
          <a href={url} download={`image-studio-${item.job_id.slice(0, 8)}-${index + 1}.webp`}>
            <Download />
            Download
          </a>
        </Button>
        <Button
          variant="outline"
          onClick={createShareLink}
          disabled={share.state === "working"}
          className="h-10 rounded-full border-plate-line bg-transparent text-plate-foreground hover:bg-white/10 hover:text-plate-foreground dark:border-plate-line dark:bg-transparent dark:hover:bg-white/10"
        >
          <Link2 />
          {share.state === "working" ? "Creating link…" : "Share link"}
        </Button>
        <Button
          variant="ghost"
          onClick={() => onReuse(item)}
          className={`h-10 rounded-full ${ON_DARK}`}
        >
          <RotateCcw />
          Use these settings
        </Button>
      </div>

      <div aria-live="polite" className="grid gap-2 text-sm">
        {share.state === "ready" && (
          <>
            <Input
              readOnly
              value={share.link.url}
              aria-label="Share link"
              onFocus={(event) => event.target.select()}
              className="h-9 border-plate-line bg-white/5 text-plate-foreground dark:bg-white/5"
            />
            <p className="text-plate-muted">
              {share.copied ? "Copied. " : ""}
              Anyone with the link can open the image until{" "}
              {new Date(share.link.expires_at).toLocaleString(undefined, {
                dateStyle: "medium",
                timeStyle: "short",
              })}
              .
            </p>
          </>
        )}
        {share.state === "failed" && <p className="text-plate-error">{share.message}</p>}
      </div>
    </aside>
  );
}

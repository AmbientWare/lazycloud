"use client";

import type { ReactNode } from "react";

import type { GalleryItem } from "./api";

export type GalleryImage = { item: GalleryItem; index: number; url: string };

export function galleryImages(items: GalleryItem[]): GalleryImage[] {
  return items.flatMap((item) => item.image_urls.map((url, index) => ({ item, index, url })));
}

// Justified rows: each tile grows in proportion to its width over its height,
// so every tile in a row ends up the same height. The filler after the last
// tile keeps a short final row from stretching.
export function GalleryGrid({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-5 [--row-height:9.5rem] after:grow-[999] after:content-[''] sm:[--row-height:13rem] lg:[--row-height:16rem]">
      {children}
    </div>
  );
}

export function Tile({
  ratio,
  caption,
  children,
}: {
  ratio: number;
  caption: string;
  children: ReactNode;
}) {
  return (
    <figure
      className="m-0 grid min-w-0 content-start gap-2"
      style={{ flexGrow: ratio, flexBasis: `calc(var(--row-height) * ${ratio})` }}
    >
      <div className="relative overflow-hidden rounded-md bg-muted" style={{ aspectRatio: ratio }}>
        {children}
      </div>
      <figcaption className="truncate font-serif text-[0.95rem] text-muted-foreground">
        {caption}
      </figcaption>
    </figure>
  );
}

export function ImageTile({
  image,
  ratio,
  onOpen,
}: {
  image: GalleryImage;
  ratio: number;
  onOpen: () => void;
}) {
  const { item, index, url } = image;
  const position = item.image_count > 1 ? ` (${index + 1} of ${item.image_count})` : "";
  return (
    <Tile ratio={ratio} caption={item.prompt}>
      <button
        type="button"
        onClick={onOpen}
        aria-label={`Open image: ${item.prompt}${position}`}
        className="group absolute inset-0 cursor-zoom-in outline-none focus-visible:ring-3 focus-visible:ring-ring focus-visible:ring-inset"
      >
        <img
          src={url}
          alt=""
          loading="lazy"
          className="size-full object-cover transition-transform duration-500 ease-out group-hover:scale-[1.03]"
        />
      </button>
    </Tile>
  );
}

"use client";

import { useState } from "react";

import type { GalleryItem, ShareLink } from "./api";

export function Gallery({
  items,
  onShare,
  onReuse,
}: {
  items: GalleryItem[];
  onShare: (jobId: string, index: number) => Promise<ShareLink>;
  onReuse: (item: GalleryItem) => void;
}) {
  if (items.length === 0) {
    return <p className="muted empty">Finished images appear here.</p>;
  }
  return (
    <section className="gallery">
      {items.map((item) => (
        <GalleryCard key={item.job_id} item={item} onShare={onShare} onReuse={onReuse} />
      ))}
    </section>
  );
}

function GalleryCard({
  item,
  onShare,
  onReuse,
}: {
  item: GalleryItem;
  onShare: (jobId: string, index: number) => Promise<ShareLink>;
  onReuse: (item: GalleryItem) => void;
}) {
  const [note, setNote] = useState<string | null>(null);

  const share = async (index: number) => {
    setNote("Creating a share link…");
    try {
      const link = await onShare(item.job_id, index);
      await navigator.clipboard.writeText(link.url);
      setNote(`Link copied. It works until ${new Date(link.expires_at).toLocaleString()}.`);
    } catch (error) {
      setNote(error instanceof Error ? error.message : "Sharing failed");
    }
  };

  return (
    <article className="panel card">
      <div className={`images count-${item.image_count}`}>
        {item.image_urls.map((url, index) => (
          <figure key={url}>
            <a href={url} target="_blank" rel="noreferrer">
              <img src={url} alt={item.prompt} loading="lazy" />
            </a>
            <button type="button" className="overlay" onClick={() => share(index)}>
              Share
            </button>
          </figure>
        ))}
      </div>
      <p className="prompt">{item.prompt}</p>
      <p className="muted meta">
        {item.style} · {item.aspect} · seed {item.seed} ·{" "}
        {new Date(item.created_at).toLocaleString()}
      </p>
      <div className="row">
        <button type="button" onClick={() => onReuse(item)}>
          Use these settings
        </button>
        {note && <small className="muted">{note}</small>}
      </div>
    </article>
  );
}

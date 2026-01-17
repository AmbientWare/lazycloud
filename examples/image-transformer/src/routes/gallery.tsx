import { createFileRoute } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/react-start";
import { Button } from "@/components/ui/button";
import { getRecentTransformations, deleteTransformation } from "@/lib/db";
import { deleteImages, getImageAsDataUrl } from "@/lib/storage";
import { STYLES } from "@/lib/replicate";
import { PageLayout } from "@/components/screens";
import { Wand2, Trash2, ArrowLeft, ImageIcon } from "lucide-react";
import { useState } from "react";

type GalleryItem = {
  id: string;
  style: string;
  created_at: string;
  originalDataUrl: string | null;
  transformedDataUrl: string | null;
};

const getGallery = createServerFn({ method: "GET" }).handler(async () => {
  const transformations = getRecentTransformations(50);
  return transformations.map((t) => ({
    id: t.id,
    style: t.style,
    created_at: t.created_at,
    originalDataUrl: getImageAsDataUrl(t.original_path),
    transformedDataUrl: t.transformed_path ? getImageAsDataUrl(t.transformed_path) : null,
  }));
});

const deleteItem = createServerFn({ method: "POST" })
  .inputValidator((data: { id: string }) => data)
  .handler(async ({ data }) => {
    deleteImages(data.id);
    deleteTransformation(data.id);
    return { success: true };
  });

export const Route = createFileRoute("/gallery")({
  component: Gallery,
  loader: async () => {
    return { transformations: await getGallery() };
  },
});

function Gallery() {
  const { transformations: initialTransformations } = Route.useLoaderData();
  const [transformations, setTransformations] = useState<GalleryItem[]>(initialTransformations);
  const [deleting, setDeleting] = useState<string | null>(null);

  const handleDelete = async (id: string) => {
    setDeleting(id);
    try {
      await deleteItem({ data: { id } });
      setTransformations((prev) => prev.filter((t) => t.id !== id));
    } finally {
      setDeleting(null);
    }
  };

  return (
    <PageLayout
      maxWidth="6xl"
      action={
        <Button variant="ghost" asChild>
          <a href="/">
            <ArrowLeft className="h-4 w-4" />
            New Transform
          </a>
        </Button>
      }
    >
      <div className="mb-8">
        <h1 className="text-3xl font-bold mb-2">Gallery</h1>
        <p className="text-muted-foreground">
          Your transformation history ({transformations.length} images)
        </p>
      </div>

      {transformations.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {transformations.map((item) => (
            <GalleryCard
              key={item.id}
              item={item}
              onDelete={handleDelete}
              isDeleting={deleting === item.id}
            />
          ))}
        </div>
      )}
    </PageLayout>
  );
}

function EmptyState() {
  return (
    <div className="text-center py-16">
      <div className="inline-flex h-16 w-16 items-center justify-center rounded-full bg-muted mb-4">
        <ImageIcon className="h-8 w-8 text-muted-foreground" />
      </div>
      <h2 className="text-xl font-semibold mb-2">No transformations yet</h2>
      <p className="text-muted-foreground mb-6">
        Transform your first image to see it here
      </p>
      <Button asChild>
        <a href="/">
          <Wand2 className="h-4 w-4" />
          Transform an Image
        </a>
      </Button>
    </div>
  );
}

interface GalleryCardProps {
  item: GalleryItem;
  onDelete: (id: string) => void;
  isDeleting: boolean;
}

function GalleryCard({ item, onDelete, isDeleting }: GalleryCardProps) {
  return (
    <div className="group relative rounded-xl border bg-card overflow-hidden">
      <div className="aspect-video relative">
        <div className="absolute inset-0 grid grid-cols-2">
          {item.originalDataUrl && (
            <img
              src={item.originalDataUrl}
              alt="Original"
              className="w-full h-full object-cover"
            />
          )}
          {item.transformedDataUrl && (
            <img
              src={item.transformedDataUrl}
              alt="Transformed"
              className="w-full h-full object-cover"
            />
          )}
        </div>
        <div className="absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-t from-black/60 to-transparent" />
      </div>

      <div className="absolute bottom-0 left-0 right-0 p-4 text-white">
        <div className="flex items-center justify-between">
          <div>
            <p className="font-medium">
              {STYLES[item.style as keyof typeof STYLES]?.name || item.style}
            </p>
            <p className="text-sm text-white/70">
              {new Date(item.created_at).toLocaleDateString()}
            </p>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="text-white/70 hover:text-white hover:bg-white/20"
            onClick={() => onDelete(item.id)}
            disabled={isDeleting}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  );
}

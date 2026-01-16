import { X, ZoomIn } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

interface ImagePreviewProps {
  src: string;
  alt?: string;
  onRemove?: () => void;
  className?: string;
}

export function ImagePreview({ src, alt = "Image preview", onRemove, className }: ImagePreviewProps) {
  return (
    <div className={cn("relative group rounded-xl overflow-hidden bg-muted", className)}>
      <img src={src} alt={alt} className="w-full h-auto block" />

      {/* Overlay on hover */}
      <div className="absolute inset-0 bg-black/0 group-hover:bg-black/20 transition-colors" />

      {/* Remove button */}
      {onRemove && (
        <Button
          variant="secondary"
          size="icon"
          onClick={onRemove}
          className="absolute top-3 right-3 h-8 w-8 rounded-full opacity-0 group-hover:opacity-100 transition-opacity shadow-lg"
        >
          <X className="h-4 w-4" />
        </Button>
      )}

      {/* Zoom indicator */}
      <div className="absolute bottom-3 right-3 flex items-center gap-1.5 px-2.5 py-1.5 rounded-full bg-black/60 text-white text-xs font-medium backdrop-blur-sm opacity-0 group-hover:opacity-100 transition-opacity">
        <ZoomIn className="h-3 w-3" />
        Preview
      </div>
    </div>
  );
}

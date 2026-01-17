import { useCallback } from "react";
import { useDropzone } from "react-dropzone";
import { Upload, Image as ImageIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface DropzoneProps {
  onImageSelect: (dataUrl: string, filename: string) => void;
  className?: string;
}

export function Dropzone({ onImageSelect, className }: DropzoneProps) {
  const onDrop = useCallback(
    (acceptedFiles: File[]) => {
      const file = acceptedFiles[0];
      if (file) {
        const reader = new FileReader();
        reader.onload = () => {
          onImageSelect(reader.result as string, file.name);
        };
        reader.readAsDataURL(file);
      }
    },
    [onImageSelect]
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { "image/*": [".png", ".jpg", ".jpeg", ".gif", ".webp"] },
    maxFiles: 1,
  });

  return (
    <div
      {...getRootProps()}
      className={cn(
        "group relative overflow-hidden rounded-2xl border-2 border-dashed transition-all duration-300 cursor-pointer",
        isDragActive
          ? "border-primary bg-primary/5 scale-[1.01]"
          : "border-border hover:border-primary/50 hover:bg-muted/50",
        className
      )}
    >
      <input {...getInputProps()} />

      {/* Background pattern */}
      <div className="absolute inset-0 opacity-[0.02] pointer-events-none">
        <div className="absolute inset-0" style={{
          backgroundImage: `url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23000000' fill-opacity='1'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E")`,
        }} />
      </div>

      <div className="relative flex flex-col items-center justify-center py-16 px-8">
        <div
          className={cn(
            "mb-6 flex h-20 w-20 items-center justify-center rounded-full transition-all duration-300",
            isDragActive
              ? "bg-primary text-primary-foreground scale-110"
              : "bg-muted text-muted-foreground group-hover:bg-primary/10 group-hover:text-primary"
          )}
        >
          {isDragActive ? (
            <ImageIcon className="h-10 w-10" />
          ) : (
            <Upload className="h-10 w-10" />
          )}
        </div>

        <div className="text-center">
          <p className="text-lg font-semibold mb-1">
            {isDragActive ? "Drop your image" : "Upload an image"}
          </p>
          <p className="text-sm text-muted-foreground mb-4">
            Drag and drop or click to browse
          </p>
          <div className="inline-flex items-center gap-2 text-xs text-muted-foreground/70">
            <span className="px-2 py-1 rounded bg-muted">PNG</span>
            <span className="px-2 py-1 rounded bg-muted">JPG</span>
            <span className="px-2 py-1 rounded bg-muted">GIF</span>
            <span className="px-2 py-1 rounded bg-muted">WebP</span>
          </div>
        </div>
      </div>
    </div>
  );
}

import { useState, useRef, useCallback, useEffect } from "react";
import { cn } from "@/lib/utils";

interface ComparisonSliderProps {
  originalImage: string;
  transformedImage: string;
  originalLabel?: string;
  transformedLabel?: string;
  className?: string;
}

export function ComparisonSlider({
  originalImage,
  transformedImage,
  originalLabel = "Original",
  transformedLabel = "Transformed",
  className,
}: ComparisonSliderProps) {
  const [position, setPosition] = useState(50);
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleMove = useCallback(
    (clientX: number) => {
      if (!containerRef.current) return;

      const rect = containerRef.current.getBoundingClientRect();
      const x = clientX - rect.left;
      const percentage = Math.max(0, Math.min(100, (x / rect.width) * 100));
      setPosition(percentage);
    },
    []
  );

  const handleMouseDown = () => setIsDragging(true);
  const handleMouseUp = () => setIsDragging(false);

  const handleMouseMove = useCallback(
    (e: MouseEvent) => {
      if (isDragging) {
        handleMove(e.clientX);
      }
    },
    [isDragging, handleMove]
  );

  const handleTouchMove = useCallback(
    (e: TouchEvent) => {
      if (isDragging) {
        handleMove(e.touches[0].clientX);
      }
    },
    [isDragging, handleMove]
  );

  useEffect(() => {
    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    document.addEventListener("touchmove", handleTouchMove);
    document.addEventListener("touchend", handleMouseUp);

    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
      document.removeEventListener("touchmove", handleTouchMove);
      document.removeEventListener("touchend", handleMouseUp);
    };
  }, [handleMouseMove, handleTouchMove]);

  return (
    <div
      ref={containerRef}
      className={cn(
        "relative overflow-hidden rounded-xl select-none cursor-ew-resize",
        className
      )}
      onMouseDown={handleMouseDown}
      onTouchStart={handleMouseDown}
    >
      {/* Transformed image (background) */}
      <img
        src={transformedImage}
        alt={transformedLabel}
        className="w-full h-auto block"
        draggable={false}
      />

      {/* Original image (clipped) */}
      <div
        className="absolute inset-0 overflow-hidden"
        style={{ clipPath: `inset(0 ${100 - position}% 0 0)` }}
      >
        <img
          src={originalImage}
          alt={originalLabel}
          className="w-full h-auto block"
          draggable={false}
        />
      </div>

      {/* Slider handle */}
      <div
        className="absolute top-0 bottom-0 w-1 bg-white shadow-lg"
        style={{ left: `${position}%`, transform: "translateX(-50%)" }}
      >
        {/* Handle grip */}
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-10 h-10 rounded-full bg-white shadow-lg flex items-center justify-center border-2 border-primary">
          <div className="flex gap-0.5">
            <div className="w-0.5 h-4 rounded-full bg-primary/60" />
            <div className="w-0.5 h-4 rounded-full bg-primary/60" />
          </div>
        </div>
      </div>

      {/* Labels */}
      <div className="absolute top-4 left-4 px-3 py-1.5 rounded-full bg-black/60 text-white text-xs font-medium backdrop-blur-sm">
        {originalLabel}
      </div>
      <div className="absolute top-4 right-4 px-3 py-1.5 rounded-full bg-black/60 text-white text-xs font-medium backdrop-blur-sm">
        {transformedLabel}
      </div>

      {/* Instructions (shows briefly) */}
      <div className="absolute bottom-4 left-1/2 -translate-x-1/2 px-4 py-2 rounded-full bg-black/60 text-white text-xs font-medium backdrop-blur-sm pointer-events-none opacity-60">
        Drag to compare
      </div>
    </div>
  );
}

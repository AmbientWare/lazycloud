import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/** Keep placeholders out of screen-reader announcements; the list owns loading status. */
export function RowsSkeleton({
  rows,
  height,
  className,
}: {
  rows: number;
  height: string;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2 p-4", className)} aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className={cn("w-full", height)} />
      ))}
    </div>
  );
}

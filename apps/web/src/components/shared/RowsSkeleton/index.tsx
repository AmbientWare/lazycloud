import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/**
 * The placeholder a list region shows while its rows load.
 *
 * `aria-hidden` is unconditional: a pulsing bar carries nothing a reader can
 * use, and announcing one per pending row reports noise instead of progress.
 *
 * `height` stays a prop because it is what makes the placeholder match the rows
 * it stands in for; a single value would misreport the shape of every list but
 * one.
 */
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

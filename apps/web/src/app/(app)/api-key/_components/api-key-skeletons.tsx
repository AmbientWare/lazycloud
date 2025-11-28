import { Skeleton } from "@/components/ui/skeleton";

export function ApiKeySkeleton() {
  return (
    <div className="space-y-4">
      <div>
        <Skeleton className="mb-2 h-8 w-48" />
        <Skeleton className="h-4 w-96" />
      </div>
      <div className="rounded-lg border p-4">
        <Skeleton className="h-24 w-full" />
      </div>
    </div>
  );
}

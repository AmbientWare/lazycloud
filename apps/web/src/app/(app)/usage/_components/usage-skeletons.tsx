import { Skeleton } from "@/components/ui/skeleton";
import {
  StyledCard,
  StyledCardHeader,
  StyledCardContent,
} from "@/components/shared/styled-card";

export function OverviewSkeleton() {
  return (
    <StyledCard>
      <StyledCardHeader>
        <Skeleton className="h-8 w-32" />
        <Skeleton className="mt-2 h-4 w-48" />
      </StyledCardHeader>
      <StyledCardContent>
        <div className="mb-6 grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-3 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="bg-muted rounded-lg p-3">
              <Skeleton className="mb-2 h-3 w-20" />
              <Skeleton className="h-6 w-16" />
            </div>
          ))}
        </div>
        <Skeleton className="h-[250px] w-full" />
      </StyledCardContent>
    </StyledCard>
  );
}

export function WorkspaceBreakdownSkeleton() {
  return (
    <div className="w-full space-y-6">
      {Array.from({ length: 2 }).map((_, i) => (
        <StyledCard key={i}>
          <div className="p-6 pb-4">
            <div className="flex items-start justify-between">
              <div className="flex flex-col gap-1">
                <div className="flex items-center gap-3">
                  <Skeleton className="h-6 w-32" />
                  <Skeleton className="h-7 w-16 rounded-md" />
                </div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-4 w-24" />
                </div>
              </div>
              <Skeleton className="h-6 w-16 rounded-full" />
            </div>
          </div>
        </StyledCard>
      ))}
    </div>
  );
}

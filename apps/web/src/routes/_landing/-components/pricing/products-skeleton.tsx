import { Skeleton } from '@/components/ui/skeleton'
import { StyledCard, StyledCardContent } from '@/components/shared/styled-card'

export function ProductsCardsSkeleton() {
  return (
    <div className="mx-auto grid max-w-6xl grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 lg:gap-6">
      {Array.from({ length: 3 }).map((_, i) => (
        <StyledCard key={i} variant="interactive" className="h-full">
          <StyledCardContent className="flex h-full flex-col gap-4 p-4 sm:p-5 lg:p-6">
            <div className="space-y-4">
              <Skeleton className="h-8 w-32" />
              <div className="space-y-2">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="size-4/5" />
                <Skeleton className="h-4 w-3/5" />
              </div>
            </div>

            <div className="flex flex-1 flex-col justify-end">
              <Skeleton className="h-11 w-full rounded-md" />
            </div>
          </StyledCardContent>
        </StyledCard>
      ))}
    </div>
  )
}

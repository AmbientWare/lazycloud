import { Skeleton } from '@/components/ui/skeleton'
import {
  StyledCard,
  StyledCardHeader,
  StyledCardContent,
  StyledCardTitle,
  StyledCardDescription,
} from '@/components/shared/styled-card'
import { SectionDivider } from '@/components/shared/section-divider'
import {
  SectionHeader,
  SectionIndicator,
} from '@/components/shared/section-header'

const METRIC_LABELS = [
  'Total',
  'CPU',
  'Memory',
  'Storage',
  'Build',
  'Endpoints',
]

export function OverviewSkeleton() {
  return (
    <StyledCard className="border-l-4 border-l-lazycloud/60 shadow-md">
      <StyledCardHeader>
        <div className="mb-1 flex items-center gap-2">
          <SectionIndicator size="md" />
          <StyledCardTitle className="text-xl font-semibold">
            Usage Trends
          </StyledCardTitle>
        </div>
        <StyledCardDescription className="text-sm">
          <Skeleton className="h-4 w-32" />
        </StyledCardDescription>
      </StyledCardHeader>
      <StyledCardContent>
        <div className="space-y-4">
          {/* Chart skeleton */}
          <div className="rounded-lg border border-border/50 bg-muted p-3">
            <h3 className="mb-2 text-sm font-semibold">Daily Cost</h3>
            <Skeleton className="h-[120px] w-full sm:h-[140px]" />
          </div>

          {/* Metric cards with real labels */}
          <div className="grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-3 lg:grid-cols-6">
            {METRIC_LABELS.map((label, i) => (
              <div
                key={label}
                className={`rounded-lg border bg-muted p-3 ${
                  i === 0
                    ? 'border-lazycloud/40 bg-gradient-to-br from-lazycloud/5 to-muted shadow-md shadow-lazycloud/10'
                    : 'border-border/40'
                }`}
              >
                <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  {label}
                </div>
                <Skeleton
                  className={`h-6 w-16 ${i === 0 ? 'bg-lazycloud/20' : ''}`}
                />
                {i !== 0 && <Skeleton className="mt-1 h-3 w-20" />}
              </div>
            ))}
          </div>
        </div>
      </StyledCardContent>
    </StyledCard>
  )
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
  )
}

/**
 * Full page skeleton for the usage page (used with pendingComponent)
 */
export function UsagePageSkeleton() {
  return (
    <>
      <OverviewSkeleton />
      <SectionDivider spacing="lg">
        <div className="space-y-5">
          <SectionHeader
            title="Workspace Breakdown"
            description="Detailed usage metrics by workspace"
            indicatorSize="lg"
            titleSize="xl"
          />
          <WorkspaceBreakdownSkeleton />
        </div>
      </SectionDivider>
    </>
  )
}

import { Skeleton } from '@/components/ui/skeleton'
import {
  StyledCard,
  StyledCardContent,
  StyledCardHeader,
} from '@/components/shared/styled-card'
import { SectionHeader } from '@/components/shared/section-header'

const DEPLOYMENTS_HEADER = {
  title: 'Deployments',
  description: 'View deployments, services, and volumes in this workspace',
  titleSize: 'xl' as const,
}

export function DeploymentsSkeleton() {
  return (
    <StyledCard className="border-l-2 border-l-lazycloud/40 shadow-md">
      <StyledCardHeader>
        <SectionHeader {...DEPLOYMENTS_HEADER} />
      </StyledCardHeader>
      <StyledCardContent>
        <div className="space-y-3">
          <Skeleton className="h-20 w-full rounded-lg" />
          <Skeleton className="h-20 w-full rounded-lg" />
        </div>
      </StyledCardContent>
    </StyledCard>
  )
}

/**
 * Full page skeleton for the workspaces page (used with pendingComponent)
 */
export function WorkspacesPageSkeleton() {
  return (
    <div className="space-y-6">
      <DeploymentsSkeleton />
    </div>
  )
}

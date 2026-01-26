import { useState, useEffect } from 'react'
import { SectionHeader } from '@/components/shared/section-header'
import { getDeploymentStatus } from '@/server/functions/deployments'
import type { DeploymentWithStatus } from '@/interfaces/deployments'
import { Accordion } from '@/components/ui/accordion'
import { Rocket } from 'lucide-react'
import {
  StyledCard,
  StyledCardContent,
  StyledCardHeader,
} from '@/components/shared/styled-card'
import { DeploymentsSkeleton } from './workspaces-skeletons'
import { DeploymentCard } from './deployment-card'

interface WorkspaceOverviewProps {
  deployments: DeploymentWithStatus[]
  isLoading: boolean
  onDeploymentsChange?: (deployments: DeploymentWithStatus[]) => void
}

const DEPLOYMENTS_HEADER = {
  title: 'Deployments',
  description: 'View deployments, services, and volumes in this workspace',
  titleSize: 'xl' as const,
}

const STATUS_POLL_INTERVAL_MS = 5000 // 5 seconds

export function WorkspaceOverview({
  deployments,
  isLoading,
  onDeploymentsChange,
}: WorkspaceOverviewProps) {
  const [openAccordionValue, setOpenAccordionValue] = useState<
    string | undefined
  >(undefined)

  // Reset accordion when deployments change (workspace switch)
  useEffect(() => {
    setOpenAccordionValue(undefined)
  }, [deployments])

  // Poll for status updates when a deployment is expanded
  useEffect(() => {
    if (!openAccordionValue) return

    const pollStatus = async () => {
      try {
        const statusResponse = await getDeploymentStatus({
          data: { deploymentId: openAccordionValue },
        })
        // Update the deployment status via parent callback
        onDeploymentsChange?.(
          deployments.map((d) =>
            d.id === openAccordionValue
              ? { ...d, status: statusResponse.status, isLoading: false }
              : d,
          ),
        )
      } catch (error) {
        console.error(
          `Failed to poll status for deployment ${openAccordionValue}:`,
          error,
        )
      }
    }

    // Initial fetch
    void pollStatus()

    // Set up polling interval
    const intervalId = setInterval(pollStatus, STATUS_POLL_INTERVAL_MS)

    return () => clearInterval(intervalId)
  }, [openAccordionValue, deployments, onDeploymentsChange])

  const handleAccordionChange = (value: string | undefined) => {
    setOpenAccordionValue(value)
    // Mark as loading if we don't have status yet
    if (value) {
      const deployment = deployments.find((d) => d.id === value)
      if (!deployment?.status && !deployment?.isLoading) {
        onDeploymentsChange?.(
          deployments.map((d) =>
            d.id === value ? { ...d, isLoading: true } : d,
          ),
        )
      }
    }
  }

  if (isLoading) {
    return <DeploymentsSkeleton />
  }

  return (
    <StyledCard className="border-l-2 border-l-lazycloud/40 shadow-md">
      <StyledCardHeader>
        <SectionHeader {...DEPLOYMENTS_HEADER} />
      </StyledCardHeader>
      <StyledCardContent>
        {deployments.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 text-center sm:py-12">
            <div className="mb-4 flex size-12 items-center justify-center rounded-lg bg-lazycloud/10">
              <Rocket className="size-6 text-lazycloud" />
            </div>
            <h3 className="mb-2 text-lg font-semibold">No deployments yet</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              Deploy your first application to see it appear here
            </p>
          </div>
        ) : (
          <Accordion
            type="single"
            collapsible
            className="w-full space-y-2"
            value={openAccordionValue}
            onValueChange={handleAccordionChange}
          >
            {deployments.map((deployment) => (
              <DeploymentCard key={deployment.id} deployment={deployment} />
            ))}
          </Accordion>
        )}
      </StyledCardContent>
    </StyledCard>
  )
}

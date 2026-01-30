import { createFileRoute } from '@tanstack/react-router'
import { useState } from 'react'
import { Building2 } from 'lucide-react'
import { Spinner } from '@/components/shared/spinner'
import {
  WorkspaceSelector,
  WorkspaceOverview,
  MemberListWrapper,
  PendingInvitations,
  WorkspacesPageSkeleton,
} from './-components/workspaces'
import {
  getWorkspaces,
  getWorkspaceWithDeployments,
} from '@/server/functions/workspaces'
import type { Workspace } from '@/interfaces/workspaces'
import type { DeploymentWithStatus } from '@/interfaces/deployments'

export const Route = createFileRoute('/_authenticated/workspaces')({
  pendingComponent: WorkspacesPagePending,
  pendingMs: 0,
  pendingMinMs: 200,
  loader: async () => {
    let workspaces: Workspace[] = []
    try {
      workspaces = await getWorkspaces()
    } catch (error) {
      console.error('Failed to fetch workspaces:', error)
    }
    const defaultWorkspace =
      workspaces.find((w) => w.is_personal) ?? workspaces[0]

    // Fetch deployments for the default workspace to avoid client-side waterfall
    let initialDeployments: DeploymentWithStatus[] = []
    if (defaultWorkspace?.id) {
      try {
        const workspaceData = await getWorkspaceWithDeployments({
          data: { workspaceId: defaultWorkspace.id },
        })
        initialDeployments = workspaceData.deployments ?? []
      } catch (error) {
        console.error('Failed to fetch initial deployments:', error)
      }
    }

    return {
      workspaces,
      defaultWorkspaceId: defaultWorkspace?.id ?? null,
      initialDeployments,
    }
  },
  component: WorkspacesPage,
})

function WorkspacesPagePending() {
  return (
    <div className="space-y-8">
      <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <div className="flex size-10 items-center justify-center rounded-lg bg-lazycloud/10">
              <Building2 className="size-5 text-lazycloud" />
            </div>
            <h1 className="text-3xl font-bold tracking-tight">Workspaces</h1>
          </div>
          <p className="ml-[52px] text-sm text-muted-foreground">
            Manage your workspaces, deployments, and team members
          </p>
        </div>
      </div>
      <WorkspacesPageSkeleton />
    </div>
  )
}

function WorkspacesPage() {
  const {
    workspaces: initialWorkspaces,
    defaultWorkspaceId,
    initialDeployments,
  } = Route.useLoaderData()

  const [workspaces, setWorkspaces] = useState<Workspace[]>(initialWorkspaces)
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(
    defaultWorkspaceId,
  )
  const [deployments, setDeployments] =
    useState<DeploymentWithStatus[]>(initialDeployments)
  const [isDeploymentsLoading, setIsDeploymentsLoading] = useState(false)

  // Fetch deployments when workspace changes
  const handleWorkspaceChange = async (workspaceId: string) => {
    if (workspaceId === selectedWorkspaceId) return

    setSelectedWorkspaceId(workspaceId)
    setIsDeploymentsLoading(true)
    setDeployments([])

    try {
      const workspaceData = await getWorkspaceWithDeployments({
        data: { workspaceId },
      })
      setDeployments(workspaceData.deployments ?? [])
    } catch (error) {
      console.error('Failed to fetch deployments:', error)
    } finally {
      setIsDeploymentsLoading(false)
    }
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <div className="flex size-10 items-center justify-center rounded-lg bg-lazycloud/10">
              <Building2 className="size-5 text-lazycloud" />
            </div>
            <h1 className="text-3xl font-bold tracking-tight">Workspaces</h1>
          </div>
          <p className="ml-[52px] text-sm text-muted-foreground">
            Manage your workspaces, deployments, and team members
          </p>
        </div>
        <div className="sm:pt-1">
          <div className="flex items-center gap-2">
            {isDeploymentsLoading && <Spinner size="sm" />}
            <WorkspaceSelector
              initialWorkspaces={workspaces}
              currentWorkspaceId={selectedWorkspaceId ?? undefined}
              onWorkspaceChange={handleWorkspaceChange}
              onWorkspacesUpdate={setWorkspaces}
            />
          </div>
        </div>
      </div>

      <div className="space-y-6">
        <PendingInvitations />
        <WorkspaceOverview
          deployments={deployments}
          isLoading={isDeploymentsLoading}
          onDeploymentsChange={setDeployments}
        />
        {!workspaces.find((w) => w.id === selectedWorkspaceId)?.is_personal && (
          <MemberListWrapper
            workspaceId={selectedWorkspaceId}
            workspaces={workspaces}
          />
        )}
      </div>
    </div>
  )
}

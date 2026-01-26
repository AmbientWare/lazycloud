import { createServerFn } from '@tanstack/react-start'
import { z } from 'zod'
import { authMiddleware } from '../middleware/auth'
import lazycloudApi from '../lazycloud-api'
import { env } from '@/env'
import type {
  Workspace,
  WorkspaceMember,
  WorkspaceRole,
  WorkspaceWithDeploymentsResponse,
} from '@/interfaces/workspaces'

export const getWorkspaces = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .inputValidator(
    z
      .object({
        startDate: z.string().optional(),
        endDate: z.string().optional(),
      })
      .optional(),
  )
  .handler(async ({ context, data }): Promise<Workspace[]> => {
    return lazycloudApi.getWorkspaces(
      context.accessToken,
      data?.startDate,
      data?.endDate,
    )
  })

export const getWorkspaceWithDeployments = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
    }),
  )
  .handler(
    async ({ context, data }): Promise<WorkspaceWithDeploymentsResponse> => {
      return lazycloudApi.getWorkspaceWithDeployments(
        context.accessToken,
        data.workspaceId,
      )
    },
  )

export const createWorkspace = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      name: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<Workspace> => {
    return lazycloudApi.createWorkspace(context.accessToken, data.name)
  })

export const deleteWorkspace = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<{ success: boolean }> => {
    return lazycloudApi.deleteWorkspace(context.accessToken, data.workspaceId)
  })

export const leaveWorkspace = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<{ success: boolean }> => {
    return lazycloudApi.leaveWorkspace(context.accessToken, data.workspaceId)
  })

export const getWorkspaceMembers = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<WorkspaceMember[]> => {
    return lazycloudApi.getWorkspaceMembers(
      context.accessToken,
      data.workspaceId,
    )
  })

export const inviteUser = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
      email: z.string().email(),
      role: z.enum(['owner', 'admin', 'member']).default('member'),
    }),
  )
  .handler(async ({ context, data }): Promise<{ success: boolean }> => {
    const acceptanceUrl = `${env.APP_URL}/workspaces`
    return lazycloudApi.inviteUser(
      context.accessToken,
      data.workspaceId,
      data.email,
      data.role as WorkspaceRole,
      acceptanceUrl,
    )
  })

export const updateMemberRole = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
      memberUserId: z.string(),
      role: z.enum(['owner', 'admin', 'member']),
    }),
  )
  .handler(async ({ context, data }): Promise<WorkspaceMember> => {
    return lazycloudApi.updateMemberRole(
      context.accessToken,
      data.workspaceId,
      data.memberUserId,
      data.role as WorkspaceRole,
    )
  })

export const removeMember = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
      memberUserId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<{ success: boolean }> => {
    return lazycloudApi.removeMember(
      context.accessToken,
      data.workspaceId,
      data.memberUserId,
    )
  })

export const transferOwnership = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
      newOwnerUserId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<{ success: boolean }> => {
    const acceptanceUrl = `${env.APP_URL}/workspaces`
    return lazycloudApi.transferOwnership(
      context.accessToken,
      data.workspaceId,
      data.newOwnerUserId,
      acceptanceUrl,
    )
  })

export const acceptInvitation = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      invitationId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<{ success: boolean }> => {
    return lazycloudApi.acceptInvitation(context.accessToken, data.invitationId)
  })

export const declineInvitation = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      invitationId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<{ success: boolean }> => {
    return lazycloudApi.declineInvitation(
      context.accessToken,
      data.invitationId,
    )
  })

export const getPendingInvitations = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(
    async ({
      context,
    }): Promise<
      {
        workspace_id: string
        workspace_name: string
        email: string
        role: string
        invited_by_name: string
        expires_at: string
        invitation_type?: string
        invitation_id: string
      }[]
    > => {
      return lazycloudApi.getPendingInvitations(context.accessToken)
    },
  )

export const cancelInvitation = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
      invitationId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<{ success: boolean }> => {
    return lazycloudApi.cancelInvitation(
      context.accessToken,
      data.workspaceId,
      data.invitationId,
    )
  })

export const getPendingOwnershipTransfer = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      workspaceId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<WorkspaceMember | null> => {
    return lazycloudApi.getPendingOwnershipTransfer(
      context.accessToken,
      data.workspaceId,
    )
  })

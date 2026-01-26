import { z } from 'zod'
import { DeploymentOverviewSchema } from './deployments'

export const WorkspaceRoles = {
  OWNER: 'owner',
  ADMIN: 'admin',
  MEMBER: 'member',
} as const

export const WorkspaceRoleSchema = z.enum([
  WorkspaceRoles.OWNER,
  WorkspaceRoles.ADMIN,
  WorkspaceRoles.MEMBER,
])

export const WorkspaceSchema = z.object({
  id: z.string(),
  name: z.string(),
  is_personal: z.boolean(),
  role: WorkspaceRoleSchema,
})

export const WorkspaceMemberSchema = z.object({
  user_id: z.string().nullable(),
  name: z.string().nullable(),
  email: z.string(),
  role: WorkspaceRoleSchema,
  status: z.string(),
  invitation_id: z.string().nullable().optional(),
})

export const WorkspaceWithDeploymentsResponseSchema = z.object({
  id: z.string(),
  name: z.string(),
  is_personal: z.boolean(),
  role: WorkspaceRoleSchema,
  has_more: z.boolean(),
  deployments: z.array(DeploymentOverviewSchema),
  cursor: z.string().nullable(),
})

export type Workspace = z.infer<typeof WorkspaceSchema>
export type WorkspaceRole = z.infer<typeof WorkspaceRoleSchema>
export type WorkspaceMember = z.infer<typeof WorkspaceMemberSchema>
export type WorkspaceWithDeploymentsResponse = z.infer<
  typeof WorkspaceWithDeploymentsResponseSchema
>

export function canInviteMembers(role?: WorkspaceRole): boolean {
  return role === WorkspaceRoles.OWNER || role === WorkspaceRoles.ADMIN
}

export function canManageMembers(role?: WorkspaceRole): boolean {
  return role === WorkspaceRoles.OWNER || role === WorkspaceRoles.ADMIN
}

import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { StyledCard } from '@/components/shared/styled-card'
import { StyledTooltip } from '@/components/shared/styled-tooltip'
import { Shield, User, Trash2 } from 'lucide-react'
import type { WorkspaceMember, WorkspaceRole } from '@/interfaces/workspaces'
import { canManageMembers, WorkspaceRoles } from '@/interfaces/workspaces'
import { updateMemberRole, removeMember } from '@/server/functions/workspaces'
import { ExpandConfirmButton } from '@/components/shared/expand-confirm-button'
import { useState } from 'react'

interface MemberCardProps {
  member: WorkspaceMember
  currentUserRole?: WorkspaceRole
  currentUserId?: string
  workspaceId: string
  onUpdate?: () => void
  isLoading?: boolean
}

const getRoleColor = (role: WorkspaceRole) => {
  switch (role) {
    case WorkspaceRoles.OWNER:
      return 'bg-purple-500/10 text-purple-500 border-purple-500/30'
    case WorkspaceRoles.ADMIN:
      return 'bg-blue-500/10 text-blue-500 border-blue-500/30'
    case WorkspaceRoles.MEMBER:
      return 'bg-green-500/10 text-green-500 border-green-500/30'
    default:
      return ''
  }
}

export function MemberCard({
  member,
  currentUserRole,
  currentUserId,
  workspaceId,
  onUpdate,
  isLoading = false,
}: MemberCardProps) {
  const [isUpdating, setIsUpdating] = useState(false)

  const canManage = canManageMembers(currentUserRole)
  const isOwner = member.role === WorkspaceRoles.OWNER
  const isCurrentUser = member.user_id === currentUserId
  const isInvited = member.status === 'invited'
  const canRemove = canManage && !isOwner && !isCurrentUser
  const canChangeRole = canManage && !isOwner && !isCurrentUser && !isInvited

  const handleRoleChange = async (newRole: WorkspaceRole) => {
    if (newRole === member.role || !member.user_id) return
    setIsUpdating(true)
    try {
      await updateMemberRole({
        data: {
          workspaceId,
          memberUserId: member.user_id,
          role: newRole,
        },
      })
      onUpdate?.()
    } catch (error) {
      console.error('Failed to update member role:', error)
    } finally {
      setIsUpdating(false)
    }
  }

  const handleRemove = async () => {
    try {
      if (member.user_id) {
        await removeMember({
          data: { workspaceId, memberUserId: member.user_id },
        })
      }
      onUpdate?.()
    } catch (error) {
      console.error('Failed to remove member:', error)
    }
  }

  if (isLoading) {
    return (
      <StyledCard
        variant="minimal"
        className="gap-0 overflow-hidden border-muted p-4"
      >
        <div className="flex items-center justify-between gap-3">
          <Skeleton className="h-5 w-64" />
          <Skeleton className="h-8 w-24" />
        </div>
      </StyledCard>
    )
  }

  const hasName = member.name?.trim()

  return (
    <StyledCard
      variant="minimal"
      className="gap-0 overflow-hidden border-border/50 bg-muted/30 p-4 shadow-sm"
    >
      <div className="flex min-h-[32px] items-center justify-between gap-3">
        {/* Left side: Name/Email */}
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          {member.role === WorkspaceRoles.OWNER && (
            <Shield className="size-4 shrink-0 text-purple-500" />
          )}
          {member.role === WorkspaceRoles.ADMIN && (
            <User className="size-4 shrink-0 text-blue-500" />
          )}
          <div className="truncate text-sm">
            {hasName ? (
              <>
                <span className="font-medium">{member.name}</span>
                <span className="text-muted-foreground"> · {member.email}</span>
              </>
            ) : (
              <span className="font-medium">{member.email}</span>
            )}
          </div>
        </div>

        {/* Right side: Badges and actions */}
        <div className="flex shrink-0 items-center gap-2">
          {isCurrentUser && (
            <Badge variant="outline" className="shrink-0 text-xs">
              You
            </Badge>
          )}
          {canChangeRole ? (
            <Select
              value={member.role}
              onValueChange={(value) =>
                handleRoleChange(value as WorkspaceRole)
              }
              disabled={isUpdating}
            >
              <SelectTrigger className="h-11 w-full cursor-pointer text-xs sm:w-[120px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem
                  value={WorkspaceRoles.MEMBER}
                  className="cursor-pointer"
                >
                  <div className="flex items-center gap-2">
                    <User className="size-3" />
                    Member
                  </div>
                </SelectItem>
                <SelectItem
                  value={WorkspaceRoles.ADMIN}
                  className="cursor-pointer"
                >
                  <div className="flex items-center gap-2">
                    <Shield className="size-3" />
                    Admin
                  </div>
                </SelectItem>
              </SelectContent>
            </Select>
          ) : (
            <Badge
              variant="outline"
              className={`text-xs capitalize ${getRoleColor(member.role)}`}
            >
              {member.role}
            </Badge>
          )}
          {canRemove && (
            <StyledTooltip content="Remove member">
              <div>
                <ExpandConfirmButton
                  onConfirm={handleRemove}
                  icon={Trash2}
                  color="red"
                  buttonClassName="text-red-500 hover:bg-red-500/10 cursor-pointer"
                />
              </div>
            </StyledTooltip>
          )}
        </div>
      </div>
    </StyledCard>
  )
}

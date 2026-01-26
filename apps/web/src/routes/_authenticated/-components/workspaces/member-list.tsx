import { useState, useMemo, useEffect } from 'react'
import { useRouter } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { MemberCard } from './member-card'
import { InviteMembersDialog } from './invite-members-dialog'
import { TransferOwnershipDialog } from './transfer-ownership-dialog'
import { PendingInvitationsDialog } from './pending-invitations-dialog'
import {
  getWorkspaceMembers,
  getPendingOwnershipTransfer,
} from '@/server/functions/workspaces'
import { getCurrentUserInternalId } from '@/server/functions/users'
import type {
  WorkspaceMember,
  WorkspaceRole,
  Workspace,
} from '@/interfaces/workspaces'
import { WorkspaceRoles } from '@/interfaces/workspaces'
import { UserPlus, Users, Crown, Mail, MoreVertical } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { SectionHeader } from '@/components/shared/section-header'
import { SectionDivider } from '@/components/shared/section-divider'
import {
  StyledCard,
  StyledCardContent,
  StyledCardHeader,
} from '@/components/shared/styled-card'

interface MemberListWrapperProps {
  workspaceId: string | null
  workspaces?: Workspace[]
}

interface MemberListProps {
  members: WorkspaceMember[]
  currentUserRole?: WorkspaceRole
  currentUserId?: string
  workspaceId: string
  workspaces?: Workspace[]
  isPersonalWorkspace?: boolean
  isLoading?: boolean
  onReload?: () => void
}

function MemberList({
  members: initialMembers,
  currentUserRole,
  currentUserId,
  workspaceId,
  workspaces = [],
  isPersonalWorkspace = false,
  isLoading = false,
  onReload,
}: MemberListProps) {
  const router = useRouter()
  const [members, setMembers] = useState(initialMembers)
  const [searchQuery, setSearchQuery] = useState('')
  const [roleFilter, setRoleFilter] = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [inviteDialogOpen, setInviteDialogOpen] = useState(false)
  const [transferDialogOpen, setTransferDialogOpen] = useState(false)
  const [pendingInvitationsDialogOpen, setPendingInvitationsDialogOpen] =
    useState(false)

  const [hasPendingOwnershipTransfer, setHasPendingOwnershipTransfer] =
    useState(false)
  const [pendingOwnershipTransfer, setPendingOwnershipTransfer] =
    useState<WorkspaceMember | null>(null)

  useEffect(() => {
    // Filter out invited members - they'll be shown in the pending invitations dialog
    const activeMembers = initialMembers.filter(
      (m) => m.status.toLowerCase() !== 'invited',
    )
    setMembers(activeMembers)
  }, [initialMembers])

  useEffect(() => {
    // Check for pending ownership transfer if user is owner
    if (
      currentUserRole === WorkspaceRoles.OWNER &&
      !isPersonalWorkspace &&
      workspaceId
    ) {
      const checkPendingTransfer = async () => {
        try {
          const pending = await getPendingOwnershipTransfer({
            data: { workspaceId },
          })
          setHasPendingOwnershipTransfer(!!pending)
          setPendingOwnershipTransfer(pending)
        } catch (error) {
          // Silently handle 403/owner role errors - user might not be owner anymore
          const errorMessage =
            error instanceof Error ? error.message : String(error)
          const lowerErrorMessage = errorMessage.toLowerCase()
          if (
            !lowerErrorMessage.includes('owner role required') &&
            !lowerErrorMessage.includes('403') &&
            !lowerErrorMessage.includes('forbidden')
          ) {
            console.error('Failed to check pending ownership transfer:', error)
          }
          setHasPendingOwnershipTransfer(false)
          setPendingOwnershipTransfer(null)
        }
      }
      void checkPendingTransfer()
    } else {
      setHasPendingOwnershipTransfer(false)
      setPendingOwnershipTransfer(null)
    }
  }, [currentUserRole, isPersonalWorkspace, workspaceId])

  const handleUpdate = async () => {
    if (onReload) {
      onReload()
    } else {
      router.invalidate()
    }
    // Also refresh pending ownership transfer status
    if (
      currentUserRole === WorkspaceRoles.OWNER &&
      !isPersonalWorkspace &&
      workspaceId
    ) {
      try {
        const pending = await getPendingOwnershipTransfer({
          data: { workspaceId },
        })
        setHasPendingOwnershipTransfer(!!pending)
        setPendingOwnershipTransfer(pending)
      } catch (error) {
        const errorMessage =
          error instanceof Error ? error.message : String(error)
        const lowerErrorMessage = errorMessage.toLowerCase()
        if (
          !lowerErrorMessage.includes('owner role required') &&
          !lowerErrorMessage.includes('403') &&
          !lowerErrorMessage.includes('forbidden')
        ) {
          console.error('Failed to check pending ownership transfer:', error)
        }
        setHasPendingOwnershipTransfer(false)
        setPendingOwnershipTransfer(null)
      }
    }
  }

  const filteredMembers = useMemo(() => {
    return members.filter((member) => {
      const matchesSearch =
        searchQuery === '' ||
        (member.name?.toLowerCase().includes(searchQuery.toLowerCase()) ??
          false) ||
        member.email.toLowerCase().includes(searchQuery.toLowerCase())

      const matchesRole = roleFilter === 'all' || member.role === roleFilter
      const matchesStatus =
        statusFilter === 'all' ||
        member.status.toLowerCase() === statusFilter.toLowerCase()

      return matchesSearch && matchesRole && matchesStatus
    })
  }, [members, searchQuery, roleFilter, statusFilter])

  const pendingInvitations = useMemo(() => {
    return initialMembers.filter((m) => m.status.toLowerCase() === 'invited')
  }, [initialMembers])

  const pendingInvitationsCount = pendingInvitations.length

  const sortedMembers = useMemo(() => {
    return [...filteredMembers].sort((a, b) => {
      const roleOrder = {
        [WorkspaceRoles.OWNER]: 0,
        [WorkspaceRoles.ADMIN]: 1,
        [WorkspaceRoles.MEMBER]: 2,
      }
      const roleDiff = (roleOrder[a.role] ?? 99) - (roleOrder[b.role] ?? 99)
      if (roleDiff !== 0) return roleDiff

      return (a.name ?? a.email).localeCompare(b.name ?? b.email)
    })
  }, [filteredMembers])

  return (
    <SectionDivider spacing="lg">
      <StyledCard className="border-l-2 border-l-lazycloud/40 shadow-md">
        <StyledCardHeader>
          <div className="flex items-center justify-between">
            <SectionHeader
              title="Members"
              description="Manage workspace members, invitations, and their roles"
              titleSize="xl"
            />
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="outline"
                  size="sm"
                  className="relative h-9 shrink-0"
                >
                  <MoreVertical className="size-4" />
                  {(pendingInvitationsCount > 0 ||
                    hasPendingOwnershipTransfer) && (
                    <span className="absolute -right-1 -top-1 flex size-5 items-center justify-center rounded-full bg-blue-500 text-[10px] font-bold text-white">
                      {pendingInvitationsCount +
                        (hasPendingOwnershipTransfer ? 1 : 0)}
                    </span>
                  )}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  onClick={() => setInviteDialogOpen(true)}
                  disabled={isPersonalWorkspace}
                  className="cursor-pointer"
                >
                  <UserPlus className="mr-2 size-4" />
                  Invite Members
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() => setPendingInvitationsDialogOpen(true)}
                  className="cursor-pointer"
                >
                  <Mail className="mr-2 size-4" />
                  View Pending Invitations
                  {pendingInvitationsCount > 0 && (
                    <span className="ml-auto text-xs text-muted-foreground">
                      ({pendingInvitationsCount})
                    </span>
                  )}
                </DropdownMenuItem>
                {currentUserRole === WorkspaceRoles.OWNER &&
                  !isPersonalWorkspace && (
                    <>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        onClick={() => setTransferDialogOpen(true)}
                        className="cursor-pointer"
                      >
                        <Crown className="mr-2 size-4" />
                        Transfer Ownership
                        {hasPendingOwnershipTransfer && (
                          <Badge
                            variant="outline"
                            className="ml-auto border-purple-500/30 bg-purple-500/10 text-xs text-purple-500"
                          >
                            Pending
                          </Badge>
                        )}
                      </DropdownMenuItem>
                    </>
                  )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </StyledCardHeader>
        <StyledCardContent>
          <div className="space-y-4">
            <div className="flex flex-col gap-3 sm:flex-row">
              <div className="flex-1">
                <Input
                  placeholder="Search by name or email..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full"
                />
              </div>
              <Select value={roleFilter} onValueChange={setRoleFilter}>
                <SelectTrigger className="w-full cursor-pointer sm:w-[180px]">
                  <SelectValue placeholder="Filter by role" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all" className="cursor-pointer">
                    All Roles
                  </SelectItem>
                  <SelectItem
                    value={WorkspaceRoles.OWNER}
                    className="cursor-pointer"
                  >
                    Owner
                  </SelectItem>
                  <SelectItem
                    value={WorkspaceRoles.ADMIN}
                    className="cursor-pointer"
                  >
                    Admin
                  </SelectItem>
                  <SelectItem
                    value={WorkspaceRoles.MEMBER}
                    className="cursor-pointer"
                  >
                    Member
                  </SelectItem>
                </SelectContent>
              </Select>
              <Select value={statusFilter} onValueChange={setStatusFilter}>
                <SelectTrigger className="w-full cursor-pointer sm:w-[180px]">
                  <SelectValue placeholder="Filter by status" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all" className="cursor-pointer">
                    All Status
                  </SelectItem>
                  <SelectItem value="active" className="cursor-pointer">
                    Active
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              {isLoading &&
              members.length === 0 ? null : sortedMembers.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12 text-center">
                  <div className="mb-4 flex size-12 items-center justify-center rounded-lg bg-muted">
                    <Users className="size-6 text-muted-foreground" />
                  </div>
                  <h3 className="mb-2 text-lg font-semibold">
                    {members.length === 0
                      ? 'No members yet'
                      : 'No members match your filters'}
                  </h3>
                  <p className="max-w-md text-sm text-muted-foreground">
                    {members.length === 0
                      ? 'Invite team members to collaborate on this workspace'
                      : 'Try adjusting your search or filter criteria'}
                  </p>
                </div>
              ) : (
                sortedMembers.map((member) => (
                  <MemberCard
                    key={member.user_id ?? member.email}
                    member={member}
                    currentUserRole={currentUserRole}
                    currentUserId={currentUserId}
                    workspaceId={workspaceId}
                    onUpdate={handleUpdate}
                  />
                ))
              )}
            </div>
          </div>
        </StyledCardContent>
      </StyledCard>
      <InviteMembersDialog
        open={inviteDialogOpen}
        onOpenChange={setInviteDialogOpen}
        workspaces={workspaces}
        currentWorkspaceId={workspaceId}
        currentUserRole={currentUserRole}
        onReload={handleUpdate}
      />
      <PendingInvitationsDialog
        open={pendingInvitationsDialogOpen}
        onOpenChange={setPendingInvitationsDialogOpen}
        workspaceId={workspaceId}
        invitations={pendingInvitations}
        onReload={handleUpdate}
      />
      {workspaces.length > 0 && !isPersonalWorkspace && (
        <TransferOwnershipDialog
          open={transferDialogOpen}
          onOpenChange={(open) => {
            setTransferDialogOpen(open)
            // Refresh pending transfer status when dialog closes
            if (!open && currentUserRole === WorkspaceRoles.OWNER) {
              const checkPendingTransfer = async () => {
                try {
                  const pending = await getPendingOwnershipTransfer({
                    data: { workspaceId },
                  })
                  setHasPendingOwnershipTransfer(!!pending)
                  setPendingOwnershipTransfer(pending)
                } catch (error) {
                  // Silently handle 403/owner role errors
                  const errorMessage =
                    error instanceof Error ? error.message : String(error)
                  if (
                    !errorMessage.includes('Owner role required') &&
                    !errorMessage.includes('403')
                  ) {
                    console.error(
                      'Failed to check pending ownership transfer:',
                      error,
                    )
                  }
                  setHasPendingOwnershipTransfer(false)
                  setPendingOwnershipTransfer(null)
                }
              }
              void checkPendingTransfer()
            }
          }}
          members={members}
          workspaceId={workspaceId}
          workspaceName={
            workspaces.find((w) => w.id === workspaceId)?.name ?? 'Workspace'
          }
          pendingTransfer={pendingOwnershipTransfer}
          onTransferChange={handleUpdate}
        />
      )}
    </SectionDivider>
  )
}

export function MemberListWrapper({
  workspaceId,
  workspaces = [],
}: MemberListWrapperProps) {
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [currentUserRole, setCurrentUserRole] = useState<
    WorkspaceRole | undefined
  >()
  const [currentUserId, setCurrentUserId] = useState<string | undefined>()
  const [isLoading, setIsLoading] = useState(true)

  const loadMembers = async () => {
    if (!workspaceId) {
      setIsLoading(true)
      setMembers([])
      setCurrentUserRole(undefined)
      setCurrentUserId(undefined)
      return
    }

    setIsLoading(true)
    try {
      const [currentUserInternalId, membersData] = await Promise.all([
        getCurrentUserInternalId(),
        getWorkspaceMembers({ data: { workspaceId } }),
      ])

      const currentUserMember = membersData.find(
        (m) => m.user_id === currentUserInternalId,
      )

      setCurrentUserId(currentUserInternalId)
      setMembers(membersData)
      setCurrentUserRole(currentUserMember?.role)
    } catch (error) {
      console.error('Failed to load workspace members:', error)
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false

    const loadMembersWithCancel = async () => {
      if (!workspaceId) {
        setIsLoading(true)
        setMembers([])
        setCurrentUserRole(undefined)
        setCurrentUserId(undefined)
        return
      }

      setIsLoading(true)
      try {
        const [currentUserInternalId, membersData] = await Promise.all([
          getCurrentUserInternalId(),
          getWorkspaceMembers({ data: { workspaceId } }),
        ])

        if (cancelled) return

        const currentUserMember = membersData.find(
          (m) => m.user_id === currentUserInternalId,
        )

        if (cancelled) return

        setCurrentUserId(currentUserInternalId)
        setMembers(membersData)
        setCurrentUserRole(currentUserMember?.role)
      } catch (error) {
        if (cancelled) return
        console.error('Failed to load workspace members:', error)
      } finally {
        if (!cancelled) {
          setIsLoading(false)
        }
      }
    }

    void loadMembersWithCancel()

    return () => {
      cancelled = true
    }
  }, [workspaceId])

  const currentWorkspace = workspaces.find((w) => w.id === workspaceId)

  return workspaceId ? (
    <MemberList
      members={members}
      currentUserRole={currentUserRole}
      currentUserId={currentUserId}
      workspaceId={workspaceId}
      workspaces={workspaces}
      isPersonalWorkspace={currentWorkspace?.is_personal ?? false}
      isLoading={isLoading}
      onReload={loadMembers}
    />
  ) : null
}

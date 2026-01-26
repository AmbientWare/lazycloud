import { useState, useEffect } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { Mail, Trash2, Send } from 'lucide-react'
import { cancelInvitation, inviteUser } from '@/server/functions/workspaces'
import type { WorkspaceMember, WorkspaceRole } from '@/interfaces/workspaces'
import { WorkspaceRoles } from '@/interfaces/workspaces'
import {
  StyledCard,
  StyledCardContent,
  StyledCardHeader,
} from '@/components/shared/styled-card'
import { ExpandConfirmButton } from '@/components/shared/expand-confirm-button'
import { StyledTooltip } from '@/components/shared/styled-tooltip'
import { toast } from 'sonner'

interface PendingInvitationsDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  workspaceId: string
  invitations: WorkspaceMember[]
  onReload?: () => void
}

export function PendingInvitationsDialog({
  open,
  onOpenChange,
  workspaceId,
  invitations: initialInvitations,
  onReload,
}: PendingInvitationsDialogProps) {
  const [invitations, setInvitations] =
    useState<WorkspaceMember[]>(initialInvitations)
  const [deletingEmail, setDeletingEmail] = useState<string | null>(null)
  const [resendingEmail, setResendingEmail] = useState<string | null>(null)

  // Update invitations when prop changes
  useEffect(() => {
    setInvitations(initialInvitations)
  }, [initialInvitations])

  const handleDelete = async (invitation: WorkspaceMember) => {
    // Check if invitation_id exists and is not empty
    const invitationId = invitation.invitation_id
    if (
      !invitationId ||
      (typeof invitationId === 'string' && invitationId.trim() === '')
    ) {
      toast.error(`Invitation ID not available for ${invitation.email}`)
      return
    }
    setDeletingEmail(invitation.email)
    const toastId = toast.loading('Cancelling invitation...')
    try {
      await cancelInvitation({
        data: { workspaceId, invitationId },
      })
      toast.success('Invitation cancelled successfully.', { id: toastId })
      // Remove from local state immediately
      setInvitations((prev) =>
        prev.filter((inv) => inv.invitation_id !== invitation.invitation_id),
      )
      onReload?.()
    } catch (error) {
      console.error('Failed to cancel invitation:', error)
      const errorMessage =
        error instanceof Error
          ? error.message
          : 'Failed to cancel invitation. Please try again.'
      toast.error(errorMessage, { id: toastId })
      // Reload to refresh the list in case invitation was already accepted
      onReload?.()
    } finally {
      setDeletingEmail(null)
    }
  }

  const handleResend = async (email: string, role: WorkspaceRole) => {
    setResendingEmail(email)
    const toastId = toast.loading('Resending invitation...')
    try {
      await inviteUser({
        data: { workspaceId, email, role },
      })
      toast.success('Invitation resent successfully.', { id: toastId })
      // Reload to get updated invitation list
      onReload?.()
    } catch (error) {
      console.error('Failed to resend invitation:', error)
      const errorMessage =
        error instanceof Error
          ? error.message
          : 'Failed to resend invitation. Please try again.'
      toast.error(errorMessage, { id: toastId })
    } finally {
      setResendingEmail(null)
    }
  }

  const getRoleColor = (role: string) => {
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

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Mail className="size-5" />
            Pending Invitations
          </DialogTitle>
          <DialogDescription>
            View and manage invitations that have been sent but not yet accepted
          </DialogDescription>
        </DialogHeader>
        <div className="mt-4">
          {invitations.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <div className="mb-4 flex size-12 items-center justify-center rounded-lg bg-muted">
                <Mail className="size-6 text-muted-foreground" />
              </div>
              <h3 className="mb-2 text-lg font-semibold">
                No pending invitations
              </h3>
              <p className="max-w-md text-sm text-muted-foreground">
                All invitations have been accepted or there are no pending
                invitations for this workspace
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {invitations.map((invitation) => (
                <StyledCard
                  key={invitation.user_id ?? invitation.email}
                  variant="minimal"
                  className="gap-0 overflow-hidden border-border/50 bg-muted/30 py-2 shadow-sm"
                >
                  <StyledCardHeader>
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <div className="truncate font-medium">
                          {invitation.name ?? invitation.email}
                        </div>
                        <Badge
                          variant="outline"
                          className={`text-xs capitalize ${getRoleColor(invitation.role)}`}
                        >
                          {invitation.role}
                        </Badge>
                        <Badge
                          variant="outline"
                          className="border-yellow-500/30 bg-yellow-500/10 text-xs text-yellow-500"
                        >
                          Pending
                        </Badge>
                      </div>
                      <div className="flex items-center gap-2">
                        <StyledTooltip content="Resend invitation">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() =>
                              handleResend(invitation.email, invitation.role)
                            }
                            disabled={resendingEmail === invitation.email}
                            className="size-11 shrink-0"
                            aria-label="Resend invitation"
                          >
                            <Send
                              className={`size-4 ${
                                resendingEmail === invitation.email
                                  ? 'animate-spin'
                                  : ''
                              }`}
                            />
                          </Button>
                        </StyledTooltip>
                        <StyledTooltip content="Cancel invitation">
                          <div>
                            <ExpandConfirmButton
                              onConfirm={() => handleDelete(invitation)}
                              icon={Trash2}
                              color="red"
                              className={
                                deletingEmail === invitation.email
                                  ? 'opacity-50 cursor-not-allowed'
                                  : ''
                              }
                            />
                          </div>
                        </StyledTooltip>
                      </div>
                    </div>
                  </StyledCardHeader>
                  <StyledCardContent className="p-0">
                    <Separator />
                    <div className="px-4 py-2">
                      <div className="text-xs text-muted-foreground">
                        {invitation.email}
                      </div>
                    </div>
                  </StyledCardContent>
                </StyledCard>
              ))}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

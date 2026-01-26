import { useState, useEffect } from 'react'
import { useRouter } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { Check, Crown, X, Loader2, XCircle } from 'lucide-react'
import { toast } from 'sonner'
import {
  StyledCard,
  StyledCardContent,
  StyledCardHeader,
} from '@/components/shared/styled-card'
import {
  getPendingInvitations,
  acceptInvitation,
  declineInvitation,
} from '@/server/functions/workspaces'
import { SectionHeader } from '@/components/shared/section-header'

interface PendingInvitation {
  workspace_id: string
  workspace_name: string
  email: string
  role: string
  invited_by_name: string
  expires_at: string
  invitation_type?: string
  invitation_id: string
}

export function PendingInvitations() {
  const router = useRouter()
  const [invitations, setInvitations] = useState<PendingInvitation[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [acceptingId, setAcceptingId] = useState<string | null>(null)
  const [decliningId, setDecliningId] = useState<string | null>(null)

  useEffect(() => {
    const loadInvitations = async () => {
      try {
        const data = await getPendingInvitations()
        setInvitations(data ?? [])
      } catch (error) {
        console.error('Failed to load pending invitations:', error)
        setInvitations([])
      } finally {
        setIsLoading(false)
      }
    }

    void loadInvitations()
  }, [])

  const handleAccept = async (invitationId: string) => {
    setAcceptingId(invitationId)
    const toastId = toast.loading('Accepting invitation...')
    try {
      await acceptInvitation({
        data: { invitationId },
      })
      toast.success('Invitation accepted successfully!', { id: toastId })
      // Remove the accepted invitation from the list
      setInvitations((prev) =>
        prev.filter((inv) => inv.invitation_id !== invitationId),
      )
      // Refresh the page to show updated workspace list
      router.invalidate()
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to accept invitation',
        { id: toastId },
      )
    } finally {
      setAcceptingId(null)
    }
  }

  const handleDecline = async (invitationId: string) => {
    setDecliningId(invitationId)
    const toastId = toast.loading('Declining invitation...')
    try {
      await declineInvitation({
        data: { invitationId },
      })
      toast.success('Invitation declined', { id: toastId })
      // Remove the declined invitation from the list
      setInvitations((prev) =>
        prev.filter((inv) => inv.invitation_id !== invitationId),
      )
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to decline invitation',
        { id: toastId },
      )
    } finally {
      setDecliningId(null)
    }
  }

  const handleDismiss = () => {
    // Just hide the component for now
    setInvitations([])
  }

  if (isLoading || invitations.length === 0) {
    return null
  }

  return (
    <StyledCard className="border-l-2 border-l-lazycloud/40 shadow-md">
      <StyledCardHeader>
        <div className="flex items-center justify-between">
          <SectionHeader
            title="Pending Invitations"
            description="You have been invited to join these workspaces or accept ownership transfers"
            titleSize="xl"
          />
          <Button
            variant="ghost"
            size="sm"
            onClick={handleDismiss}
            className="size-11 p-0"
            aria-label="Dismiss invitations"
          >
            <X className="size-4" />
          </Button>
        </div>
      </StyledCardHeader>
      <StyledCardContent>
        <div className="space-y-3">
          {invitations.map((invitation) => {
            const isOwnershipTransfer =
              invitation.invitation_type === 'ownership_transfer'
            const expiresAt = new Date(invitation.expires_at)
            const isExpired = expiresAt < new Date()

            return (
              <div
                key={`${invitation.workspace_id}-${invitation.email}`}
                className="flex items-center justify-between rounded-lg border p-3"
              >
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    {isOwnershipTransfer && (
                      <Crown className="size-4 text-purple-600" />
                    )}
                    <span className="font-semibold">
                      {invitation.workspace_name}
                    </span>
                  </div>
                  <div className="mt-1 text-sm text-muted-foreground">
                    <span>
                      {isOwnershipTransfer
                        ? 'Ownership transfer'
                        : `Invited as ${invitation.role}`}
                    </span>
                    {' • '}
                    <span>by {invitation.invited_by_name}</span>
                  </div>
                  {isExpired && (
                    <div className="mt-1 text-xs text-destructive">Expired</div>
                  )}
                </div>
                <div className="ml-4 flex shrink-0 gap-2">
                  <Button
                    onClick={() => handleDecline(invitation.invitation_id)}
                    disabled={
                      isExpired ||
                      decliningId === invitation.invitation_id ||
                      acceptingId === invitation.invitation_id
                    }
                    variant="ghost"
                    size="sm"
                  >
                    {decliningId === invitation.invitation_id ? (
                      <>
                        <Loader2 className="mr-2 size-4 animate-spin" />
                        Declining...
                      </>
                    ) : (
                      <>
                        <XCircle className="mr-2 size-4" />
                        Decline
                      </>
                    )}
                  </Button>
                  <Button
                    onClick={() => handleAccept(invitation.invitation_id)}
                    disabled={
                      isExpired ||
                      acceptingId === invitation.invitation_id ||
                      decliningId === invitation.invitation_id
                    }
                    variant={isOwnershipTransfer ? 'default' : 'outline'}
                    size="sm"
                  >
                    {acceptingId === invitation.invitation_id ? (
                      <>
                        <Loader2 className="mr-2 size-4 animate-spin" />
                        Accepting...
                      </>
                    ) : (
                      <>
                        <Check className="mr-2 size-4" />
                        {isExpired ? 'Expired' : 'Accept'}
                      </>
                    )}
                  </Button>
                </div>
              </div>
            )
          })}
        </div>
      </StyledCardContent>
    </StyledCard>
  )
}

import { useState, useEffect } from 'react'
import { useRouter } from '@tanstack/react-router'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { AlertTriangle, Loader2, Shield, Crown, Trash2 } from 'lucide-react'
import type { WorkspaceMember } from '@/interfaces/workspaces'
import { WorkspaceRoles } from '@/interfaces/workspaces'
import {
  transferOwnership,
  cancelInvitation,
} from '@/server/functions/workspaces'
import { toast } from 'sonner'
import {
  StyledCard,
  StyledCardContent,
  StyledCardHeader,
} from '@/components/shared/styled-card'
import { Separator } from '@/components/ui/separator'
import { Badge } from '@/components/ui/badge'

interface TransferOwnershipDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  members: WorkspaceMember[]
  workspaceId: string
  workspaceName: string
  pendingTransfer?: WorkspaceMember | null
  onTransferChange?: () => void
}

export function TransferOwnershipDialog({
  open,
  onOpenChange,
  members,
  workspaceId,
  workspaceName,
  pendingTransfer: pendingTransferProp,
  onTransferChange,
}: TransferOwnershipDialogProps) {
  const router = useRouter()
  const [selectedAdminUserId, setSelectedAdminUserId] = useState<string>('')
  const [isTransferring, setIsTransferring] = useState(false)
  const [isCancelling, setIsCancelling] = useState(false)
  const [pendingTransfer, setPendingTransfer] =
    useState<WorkspaceMember | null>(pendingTransferProp ?? null)

  // Filter to only show admins (not owner, not invited, must have user_id)
  const adminMembers = members.filter(
    (member) =>
      member.role === WorkspaceRoles.ADMIN &&
      member.status === 'active' &&
      member.user_id !== null &&
      member.email,
  )

  useEffect(() => {
    // Update pending transfer from prop when it changes
    setPendingTransfer(pendingTransferProp ?? null)
  }, [pendingTransferProp])

  useEffect(() => {
    // Reset selection when dialog closes
    if (!open) {
      setSelectedAdminUserId('')
    }
  }, [open])

  const handleCancelPendingTransfer = async () => {
    if (!pendingTransfer?.invitation_id) {
      toast.error('Invitation ID not available')
      return
    }
    setIsCancelling(true)
    const toastId = toast.loading('Cancelling ownership transfer invitation...')
    try {
      await cancelInvitation({
        data: {
          workspaceId,
          invitationId: pendingTransfer.invitation_id,
        },
      })
      toast.success('Ownership transfer invitation cancelled.', { id: toastId })
      setPendingTransfer(null)
      onTransferChange?.()
      router.invalidate()
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : 'Failed to cancel invitation. Please try again.',
        { id: toastId },
      )
    } finally {
      setIsCancelling(false)
    }
  }

  const handleTransfer = async () => {
    if (!selectedAdminUserId) {
      toast.error('Please select an admin to transfer ownership to')
      return
    }

    setIsTransferring(true)
    const toastId = toast.loading('Sending ownership transfer invitation...')
    try {
      await transferOwnership({
        data: {
          workspaceId,
          newOwnerUserId: selectedAdminUserId,
        },
      })
      toast.success(
        'Ownership transfer invitation sent successfully. The admin will receive an email to accept the transfer.',
        { id: toastId },
      )
      // Close dialog immediately without waiting for refresh
      onOpenChange(false)
      // Notify parent to refresh pending transfer status
      onTransferChange?.()
      // Reset state after dialog closes to prevent flash
      setTimeout(() => {
        setSelectedAdminUserId('')
        router.invalidate()
      }, 100)
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : 'Failed to send ownership transfer invitation. Please try again.',
        { id: toastId },
      )
    } finally {
      setIsTransferring(false)
    }
  }

  const handleCancel = () => {
    onOpenChange(false)
    // Reset state will happen in useEffect when open becomes false
  }

  return (
    <Dialog open={open} onOpenChange={handleCancel}>
      <DialogContent className="sm:max-w-[500px] [&>button[data-slot='dialog-close']]:cursor-pointer">
        <DialogHeader>
          <DialogTitle>Transfer Workspace Ownership</DialogTitle>
          <DialogDescription>
            Transfer ownership of <strong>{workspaceName}</strong> to an admin.
            You will become an admin after the transfer is accepted.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 py-4">
          {pendingTransfer ? (
            <StyledCard
              variant="minimal"
              className="gap-0 overflow-hidden border-purple-500/30 bg-purple-500/10 py-2 shadow-sm"
            >
              <StyledCardHeader>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Crown className="size-4 text-purple-500" />
                    <div className="truncate font-medium">
                      {pendingTransfer.name ?? pendingTransfer.email}
                    </div>
                    <Badge
                      variant="outline"
                      className="border-purple-500/30 bg-purple-500/10 text-xs text-purple-500"
                    >
                      Pending Transfer
                    </Badge>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={handleCancelPendingTransfer}
                    disabled={isCancelling}
                    className="size-11 shrink-0 p-0 text-red-500 hover:bg-red-500/10"
                    aria-label="Cancel transfer"
                  >
                    {isCancelling ? (
                      <Loader2 className="size-4 animate-spin text-red-500" />
                    ) : (
                      <Trash2 className="size-4" />
                    )}
                  </Button>
                </div>
              </StyledCardHeader>
              <StyledCardContent className="p-0">
                <Separator />
                <div className="px-4 py-2">
                  <div className="text-xs text-muted-foreground">
                    {pendingTransfer.email}
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    An ownership transfer invitation has been sent. You cannot
                    send another until this one is accepted or cancelled.
                  </p>
                </div>
              </StyledCardContent>
            </StyledCard>
          ) : adminMembers.length === 0 ? (
            <div className="rounded-md border border-yellow-500/30 bg-yellow-500/10 p-4">
              <div className="flex items-start gap-3">
                <AlertTriangle className="mt-0.5 size-5 text-yellow-600" />
                <div className="flex-1">
                  <p className="text-sm font-medium text-yellow-800">
                    No admins available
                  </p>
                  <p className="mt-1 text-sm text-yellow-700">
                    You need at least one admin member before you can transfer
                    ownership. Please promote a member to admin first.
                  </p>
                </div>
              </div>
            </div>
          ) : (
            <>
              <div className="grid gap-2">
                <label className="text-sm font-medium">
                  Select Admin to Transfer To
                </label>
                <Select
                  value={selectedAdminUserId}
                  onValueChange={setSelectedAdminUserId}
                  disabled={isTransferring}
                >
                  <SelectTrigger className="cursor-pointer">
                    <SelectValue placeholder="Choose an admin..." />
                  </SelectTrigger>
                  <SelectContent>
                    {adminMembers.map((admin) => (
                      <SelectItem
                        key={admin.user_id}
                        value={admin.user_id!}
                        className="cursor-pointer"
                      >
                        <div className="flex items-center gap-2">
                          <Shield className="size-4 text-blue-500" />
                          <span>{admin.name ?? admin.email}</span>
                          {admin.email !== admin.name && (
                            <span className="text-xs text-muted-foreground">
                              {admin.email}
                            </span>
                          )}
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="rounded-md border border-blue-500/30 bg-blue-500/10 p-4">
                <p className="text-sm">
                  <strong>Important:</strong> The selected admin will receive an
                  email invitation to accept the ownership transfer. They must
                  have a subscription plan that supports all resources in this
                  workspace.
                </p>
              </div>
            </>
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={handleCancel}
            disabled={isTransferring}
            className="cursor-pointer"
          >
            {pendingTransfer ? 'Close' : 'Cancel'}
          </Button>
          {!pendingTransfer && (
            <Button
              type="button"
              variant="secondary"
              onClick={handleTransfer}
              disabled={
                isTransferring ||
                adminMembers.length === 0 ||
                !selectedAdminUserId
              }
              className="cursor-pointer bg-lazycloud hover:bg-lazycloud/80 active:bg-lazycloud/70"
            >
              {isTransferring ? (
                <Loader2 className="animate-spin" />
              ) : (
                'Send Transfer Invitation'
              )}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

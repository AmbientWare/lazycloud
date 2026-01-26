'use client'

import { useState } from 'react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { AlertCircle, Loader2 } from 'lucide-react'

interface DeleteDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirm: () => Promise<void>
  title: string
  description: string
  itemName?: string
  requireConfirmation?: boolean
  confirmText?: string
  loadingText?: string
}

export function DeleteDialog({
  open,
  onOpenChange,
  onConfirm,
  title,
  description,
  itemName,
  requireConfirmation = false,
  confirmText = 'Delete',
  loadingText = 'Deleting...',
}: DeleteDialogProps) {
  const [isDeleting, setIsDeleting] = useState(false)
  const [confirmationInput, setConfirmationInput] = useState('')

  const canDelete = !requireConfirmation || confirmationInput === itemName

  const handleConfirm = async () => {
    if (!canDelete) return

    setIsDeleting(true)
    try {
      await onConfirm()
      onOpenChange(false)
      setConfirmationInput('')
    } catch (error) {
      console.error('Delete failed:', error)
    } finally {
      setIsDeleting(false)
    }
  }

  const handleCancel = () => {
    onOpenChange(false)
    setConfirmationInput('')
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <div className="flex items-center gap-2">
            <AlertCircle className="size-5 text-red-500" />
            <DialogTitle>{title}</DialogTitle>
          </div>
          <DialogDescription className="pt-2">{description}</DialogDescription>
        </DialogHeader>

        {requireConfirmation && itemName && (
          <div className="grid gap-2 py-4">
            <Label htmlFor="confirm">
              Type <span className="font-semibold">{itemName}</span> to confirm
            </Label>
            <Input
              id="confirm"
              value={confirmationInput}
              onChange={(e) => setConfirmationInput(e.target.value)}
              placeholder={itemName}
              disabled={isDeleting}
              autoFocus
            />
          </div>
        )}

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={handleCancel}
            disabled={isDeleting}
            className="cursor-pointer transition-all duration-200 hover:bg-accent/50 hover:border-border hover:-translate-y-0.5 active:translate-y-0"
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={handleConfirm}
            disabled={isDeleting || !canDelete}
            className="cursor-pointer transition-all duration-200 hover:bg-destructive/90 hover:shadow-md hover:shadow-destructive/20 hover:-translate-y-0.5 active:translate-y-0 active:scale-[0.98]"
          >
            {isDeleting ? (
              <>
                <Loader2 className="mr-2 size-4 animate-spin" />
                {loadingText}
              </>
            ) : (
              confirmText
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

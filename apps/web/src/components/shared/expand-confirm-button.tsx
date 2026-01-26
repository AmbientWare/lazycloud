'use client'

import { useState, forwardRef } from 'react'
import { Button } from '@/components/ui/button'
import { ConfirmButtons } from '@/components/shared/confirm-btns'
import type { LucideIcon } from 'lucide-react'

interface ExpandConfirmButtonProps {
  icon: LucideIcon
  onConfirm: () => Promise<void>
  color: 'yellow' | 'red'
  size?: 'icon' | 'icon-sm' | 'icon-lg'
  className?: string
}

export const ExpandConfirmButton = forwardRef<
  HTMLButtonElement,
  ExpandConfirmButtonProps
>(({ icon: Icon, onConfirm, color, size = 'icon', className }, ref) => {
  const [showConfirm, setShowConfirm] = useState(false)
  const [isLoading, setIsLoading] = useState(false)

  const handleConfirm = async () => {
    try {
      setIsLoading(true)
      await onConfirm()
    } catch (error) {
      console.error('Action failed:', error)
    } finally {
      setIsLoading(false)
      setShowConfirm(false)
    }
  }

  const colorClasses = {
    yellow: 'text-yellow-500 hover:bg-yellow-500/10',
    red: 'text-red-500 hover:bg-red-500/10',
  }

  if (showConfirm) {
    return (
      <ConfirmButtons
        onConfirm={handleConfirm}
        onCancel={() => setShowConfirm(false)}
        isLoading={isLoading}
        confirmColor={color}
        disabled={isLoading}
      />
    )
  }

  return (
    <Button
      ref={ref}
      variant="outline"
      size={size}
      onClick={() => setShowConfirm(true)}
      className={`${colorClasses[color]} ${className ?? ''}`}
      aria-label="Regenerate"
    >
      <Icon className="size-4" />
    </Button>
  )
})

ExpandConfirmButton.displayName = 'ExpandConfirmButton'

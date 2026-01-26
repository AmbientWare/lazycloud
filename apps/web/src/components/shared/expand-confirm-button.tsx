'use client'

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { ConfirmButtons } from '@/components/shared/confirm-btns'
import type { LucideIcon } from 'lucide-react'

interface ExpandConfirmButtonProps {
  icon: LucideIcon
  onConfirm: () => Promise<void>
  color: 'yellow' | 'red'
  buttonClassName?: string
}

export function ExpandConfirmButton({
  icon: Icon,
  onConfirm,
  color,
  buttonClassName = '',
}: ExpandConfirmButtonProps) {
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

  const confirmColorMap = {
    yellow: 'yellow',
    red: 'red',
  } as const

  return (
    <div
      className={`flex gap-1 overflow-hidden transition-all duration-300 ease-in-out ${showConfirm ? 'max-w-48 opacity-100' : 'max-w-11 opacity-100'}`}
    >
      {showConfirm ? (
        <ConfirmButtons
          onConfirm={handleConfirm}
          onCancel={() => setShowConfirm(false)}
          isLoading={isLoading}
          confirmColor={confirmColorMap[color]}
          disabled={isLoading}
        />
      ) : (
        <Button
          variant="outline"
          size="icon"
          onClick={() => setShowConfirm(true)}
          className={`size-11 shrink-0 cursor-pointer ${buttonClassName}`}
          aria-label="Confirm action"
        >
          <Icon className="size-4" />
        </Button>
      )}
    </div>
  )
}

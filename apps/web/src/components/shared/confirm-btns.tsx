import { Button } from '@/components/ui/button'
import { Loader2 } from 'lucide-react'

interface ConfirmButtonsProps {
  onConfirm: () => void
  onCancel: () => void
  isLoading?: boolean
  confirmText?: string
  confirmColor?: 'red' | 'yellow' | 'blue' | 'green'
  size?: 'sm' | 'default'
  disabled?: boolean
}

export function ConfirmButtons({
  onConfirm,
  onCancel,
  isLoading = false,
  confirmText = 'Confirm',
  confirmColor = 'red',
  size = 'sm',
  disabled = false,
}: ConfirmButtonsProps) {
  const colorClasses = {
    red: 'bg-red-500 text-white hover:bg-red-600',
    yellow: 'bg-yellow-500 text-white hover:bg-yellow-600',
    blue: 'bg-blue-500 text-white hover:bg-blue-600',
    green: 'bg-green-500 text-white hover:bg-green-600',
  }

  return (
    <div className="flex gap-1">
      <Button
        variant="outline"
        size={size}
        onClick={onConfirm}
        disabled={isLoading || disabled}
        className={`min-h-[44px] cursor-pointer text-xs ${colorClasses[confirmColor]}`}
      >
        {isLoading ? <Loader2 className="mr-1 size-3 animate-spin" /> : null}
        {confirmText}
      </Button>
      <Button
        variant="outline"
        size={size}
        onClick={onCancel}
        disabled={isLoading}
        className="min-h-[44px] cursor-pointer text-xs"
      >
        Cancel
      </Button>
    </div>
  )
}

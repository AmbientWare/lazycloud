import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

interface StepLayoutProps {
  title: string
  description: ReactNode
  features?: ReactNode
  visual: ReactNode
  /** Use "start" for step-enhance which has variable height cards */
  alignItems?: 'center' | 'start'
  className?: string
}

export function StepLayout({
  title,
  description,
  features,
  visual,
  alignItems = 'center',
  className,
}: StepLayoutProps) {
  return (
    <div
      className={cn(
        'flex flex-col gap-8 lg:flex-row lg:gap-12',
        alignItems === 'center' ? 'lg:items-center' : 'lg:items-start',
        className,
      )}
    >
      {/* Text content */}
      <div className="flex-1 space-y-4">
        <h3 className="text-2xl font-bold tracking-tight md:text-3xl">
          {title}
        </h3>
        <div className="text-muted-foreground">{description}</div>
        {features}
      </div>

      {/* Visual content */}
      <div className="flex-1">{visual}</div>
    </div>
  )
}

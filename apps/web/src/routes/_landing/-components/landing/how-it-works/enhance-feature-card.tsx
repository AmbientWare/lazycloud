import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

interface EnhanceFeatureCardProps {
  icon: LucideIcon
  label: string
  description: string
  example: string
  className?: string
}

export function EnhanceFeatureCard({
  icon: Icon,
  label,
  description,
  example,
  className,
}: EnhanceFeatureCardProps) {
  return (
    <div
      className={cn(
        'rounded-lg border border-border/60 bg-card/95 p-4 shadow-lg backdrop-blur-sm',
        className,
      )}
    >
      <div className="flex items-start gap-4">
        <div className="rounded-md bg-lazycloud/10 p-2">
          <Icon className="size-5 text-lazycloud" />
        </div>
        <div className="flex-1 space-y-1">
          <p className="font-mono text-sm font-semibold text-lazycloud">
            {label}
          </p>
          <p className="text-sm text-muted-foreground">{description}</p>
          <p className="font-mono text-xs text-muted-foreground">
            e.g. {example}
          </p>
        </div>
      </div>
    </div>
  )
}

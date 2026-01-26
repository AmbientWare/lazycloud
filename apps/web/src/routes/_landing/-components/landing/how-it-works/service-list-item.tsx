import { cn } from '@/lib/utils'

interface ServiceListItemProps {
  name: string
  cpu?: string
  mem?: string
  replicas?: number
  variant?: 'simple' | 'detailed'
  className?: string
}

export function ServiceListItem({
  name,
  cpu,
  mem,
  replicas,
  variant = 'simple',
  className,
}: ServiceListItemProps) {
  if (variant === 'simple') {
    return (
      <div className={cn('flex items-center gap-2', className)}>
        <span className="h-1.5 w-1.5 rounded-full bg-green-500" />
        <span className="text-foreground">{name}</span>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'flex items-center justify-between rounded border border-border/60 bg-muted/60 px-3 py-2',
        className,
      )}
    >
      <div className="flex items-center gap-2">
        <span className="h-1.5 w-1.5 rounded-full bg-green-500" />
        <span className="text-foreground">{name}</span>
      </div>
      {(cpu !== undefined || mem !== undefined || replicas !== undefined) && (
        <div className="flex items-center gap-4 text-[10px] text-muted-foreground">
          {cpu && (
            <span>
              CPU <span className="text-lazycloud">{cpu}</span>
            </span>
          )}
          {mem && (
            <span>
              MEM <span className="text-lazycloud">{mem}MB</span>
            </span>
          )}
          {replicas && (
            <span className="text-muted-foreground">×{replicas}</span>
          )}
        </div>
      )}
    </div>
  )
}

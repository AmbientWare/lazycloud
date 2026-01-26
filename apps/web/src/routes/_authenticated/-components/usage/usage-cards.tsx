import { forwardRef } from 'react'
import { cn } from '@/lib/utils'
import { type ComponentPropsWithoutRef } from 'react'

interface MetricCardProps extends ComponentPropsWithoutRef<'div'> {
  label: string
  value: string | number
  unit?: string
  valueClassName?: string
}

export const MetricCard = forwardRef<HTMLDivElement, MetricCardProps>(
  ({ label, value, unit, valueClassName, className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          'rounded-lg border border-border/40 bg-muted p-4 transition-colors hover:border-lazycloud/30 hover:bg-muted/80 hover:shadow-sm',
          className,
        )}
        {...props}
      >
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </p>
        <p className={cn('text-2xl font-bold text-lazycloud', valueClassName)}>
          {value}
        </p>
        {unit && <p className="mt-1 text-xs text-muted-foreground">{unit}</p>}
      </div>
    )
  },
)
MetricCard.displayName = 'MetricCard'

type SectionHeaderProps = ComponentPropsWithoutRef<'p'>

export const SectionHeader = forwardRef<
  HTMLParagraphElement,
  SectionHeaderProps
>(({ className, ...props }, ref) => {
  return (
    <p
      ref={ref}
      className={cn(
        'mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground',
        className,
      )}
      {...props}
    />
  )
})
SectionHeader.displayName = 'SectionHeader'

interface ServiceCardProps extends ComponentPropsWithoutRef<'div'> {
  name: string
  cpuHours: number
  memoryHours: number
  cost?: {
    cpu: number
    memory: number
    total: number
  }
}

export const ServiceCard = forwardRef<HTMLDivElement, ServiceCardProps>(
  ({ name, cpuHours, memoryHours, cost, className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          'rounded-lg border border-border/40 bg-muted p-3 transition-colors hover:bg-muted/80',
          className,
        )}
        {...props}
      >
        <div className="mb-2 flex items-center justify-between">
          <span className="text-xs font-semibold">{name}</span>
          {cost && (
            <span className="text-xs font-bold text-lazycloud">
              ${cost.total.toFixed(4)}
            </span>
          )}
        </div>
        <div className="flex gap-3 text-xs">
          <span className="text-muted-foreground">
            <span className="text-[10px] uppercase tracking-wide">CPU</span>{' '}
            <span className="font-bold text-foreground">
              {cpuHours.toFixed(2)}
            </span>
            h
            {cost && (
              <span className="ml-1 text-muted-foreground/70">
                (${cost.cpu.toFixed(4)})
              </span>
            )}
          </span>
          <span className="text-muted-foreground">
            <span className="text-[10px] uppercase tracking-wide">Mem</span>{' '}
            <span className="font-bold text-foreground">
              {memoryHours.toFixed(2)}
            </span>
            h
            {cost && (
              <span className="ml-1 text-muted-foreground/70">
                (${cost.memory.toFixed(4)})
              </span>
            )}
          </span>
        </div>
      </div>
    )
  },
)
ServiceCard.displayName = 'ServiceCard'

interface VolumeCardProps extends ComponentPropsWithoutRef<'div'> {
  name: string
  storageClass: string
  gbHours: number
  cost?: number
}

export const VolumeCard = forwardRef<HTMLDivElement, VolumeCardProps>(
  ({ name, storageClass, gbHours, cost, className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          'flex items-center justify-between rounded-lg border border-border/40 bg-muted p-3 transition-colors hover:bg-muted/80',
          className,
        )}
        {...props}
      >
        <div className="flex flex-col">
          <span className="text-xs font-semibold">{name}</span>
          <span className="mt-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            {storageClass === 'ebs' ? 'Standard' : 'Shared'}
          </span>
        </div>
        <div className="flex flex-col items-end gap-0.5">
          <span className="text-xs">
            <span className="font-bold text-foreground">
              {gbHours.toFixed(2)}
            </span>
            <span className="text-muted-foreground"> GB-hrs</span>
          </span>
          {cost !== undefined && (
            <span className="text-xs font-bold text-lazycloud">
              ${cost.toFixed(4)}
            </span>
          )}
        </div>
      </div>
    )
  },
)
VolumeCard.displayName = 'VolumeCard'

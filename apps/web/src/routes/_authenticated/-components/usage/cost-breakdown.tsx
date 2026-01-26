import { forwardRef } from 'react'
import { cn } from '@/lib/utils'
import type {
  ServiceCostBreakdown,
  VolumeCostBreakdown,
} from '@/interfaces/usage'

interface ServiceCostCardProps extends React.ComponentPropsWithoutRef<'div'> {
  service: ServiceCostBreakdown
}

export const ServiceCostCard = forwardRef<HTMLDivElement, ServiceCostCardProps>(
  ({ service, className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          'rounded-lg border border-border/40 bg-muted p-3 transition-colors hover:bg-muted/80',
          className,
        )}
        {...props}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="mb-1.5 flex items-center gap-2">
              <span className="truncate text-sm font-semibold">
                {service.service_name}
              </span>
              <span className="whitespace-nowrap text-xs text-muted-foreground">
                {service.percentage_of_total.toFixed(1)}% of deployment cost
              </span>
            </div>

            {/* Usage Information */}
            {(service.cpu_core_hours != null ||
              service.memory_gb_hours != null) && (
              <div className="space-y-1 rounded-md bg-muted p-2">
                <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
                  {service.cpu_core_hours != null && (
                    <>
                      <span className="text-muted-foreground">
                        CPU Usage (core-hrs):
                      </span>
                      <span className="font-medium">
                        {service.cpu_core_hours.toFixed(2)}
                      </span>
                    </>
                  )}
                  {service.memory_gb_hours != null && (
                    <>
                      <span className="text-muted-foreground">
                        Memory Usage (GB-hrs):
                      </span>
                      <span className="font-medium">
                        {service.memory_gb_hours.toFixed(2)}
                      </span>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Cost Information */}
          <div className="flex shrink-0 flex-col items-end gap-1.5">
            <div className="flex items-baseline gap-1">
              <span className="text-base font-bold text-lazycloud">
                {service.total_compute_cost.toFixed(4)}
              </span>
              <span className="text-xs text-muted-foreground">$</span>
            </div>
          </div>
        </div>
      </div>
    )
  },
)
ServiceCostCard.displayName = 'ServiceCostCard'

interface VolumeCostCardProps extends React.ComponentPropsWithoutRef<'div'> {
  volume: VolumeCostBreakdown
}

export const VolumeCostCard = forwardRef<HTMLDivElement, VolumeCostCardProps>(
  ({ volume, className, ...props }, ref) => {
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
          <span className="text-xs font-semibold">{volume.volume_name}</span>
          <span className="mt-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            {volume.storage_class} &bull;{' '}
            {volume.percentage_of_total.toFixed(1)}% of deployment cost
          </span>
        </div>
        <span className="text-xs">
          <span className="font-bold text-foreground">
            ${volume.storage_cost.toFixed(4)}
          </span>
        </span>
      </div>
    )
  },
)
VolumeCostCard.displayName = 'VolumeCostCard'

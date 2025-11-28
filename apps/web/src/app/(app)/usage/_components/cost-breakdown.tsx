import { forwardRef } from "react";
import { cn } from "@/lib/utils";
import type {
  ServiceCostBreakdown,
  VolumeCostBreakdown,
} from "@/interfaces/usage";

interface ServiceCostCardProps extends React.ComponentPropsWithoutRef<"div"> {
  service: ServiceCostBreakdown;
}

export const ServiceCostCard = forwardRef<HTMLDivElement, ServiceCostCardProps>(
  ({ service, className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "bg-muted border-border/40 hover:bg-muted/80 rounded-lg border p-3 transition-colors",
          className,
        )}
        {...props}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-sm font-semibold truncate">
                {service.service_name}
              </span>
              <span className="text-muted-foreground text-xs whitespace-nowrap">
                {service.percentage_of_total.toFixed(1)}% of deployment cost
              </span>
            </div>
            
            {/* Usage Information */}
            {(service.cpu_core_hours != null || service.memory_gb_hours != null) && (
              <div className="bg-muted rounded-md p-2 space-y-1">
                <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
                  {service.cpu_core_hours != null && (
                    <>
                      <span className="text-muted-foreground">CPU Usage (core-hrs):</span>
                      <span className="font-medium">{service.cpu_core_hours.toFixed(2)}</span>
                    </>
                  )}
                  {service.memory_gb_hours != null && (
                    <>
                      <span className="text-muted-foreground">Memory Usage (GB-hrs):</span>
                      <span className="font-medium">{service.memory_gb_hours.toFixed(2)}</span>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
          
          {/* Cost Information */}
          <div className="flex flex-col items-end gap-1.5 shrink-0">
            <div className="flex items-baseline gap-1">
              <span className="text-base font-bold text-lazycloud">
                {service.total_compute_cost.toFixed(4)}
              </span>
              <span className="text-xs text-muted-foreground">$</span>
            </div>
          </div>
        </div>
      </div>
    );
  },
);
ServiceCostCard.displayName = "ServiceCostCard";

interface VolumeCostCardProps extends React.ComponentPropsWithoutRef<"div"> {
  volume: VolumeCostBreakdown;
}

export const VolumeCostCard = forwardRef<HTMLDivElement, VolumeCostCardProps>(
  ({ volume, className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "bg-muted border-border/40 hover:bg-muted/80 flex items-center justify-between rounded-lg border p-3 transition-colors",
          className,
        )}
        {...props}
      >
        <div className="flex flex-col">
          <span className="text-xs font-semibold">{volume.volume_name}</span>
          <span className="text-muted-foreground mt-0.5 text-[10px] tracking-wide uppercase">
            {volume.storage_class} • {volume.percentage_of_total.toFixed(1)}% of deployment cost
          </span>
        </div>
        <span className="text-xs">
          <span className="text-foreground font-bold">
            ${volume.storage_cost.toFixed(4)}
          </span>
        </span>
      </div>
    );
  },
);
VolumeCostCard.displayName = "VolumeCostCard";

import { forwardRef } from "react";
import { cn } from "@/lib/utils";
import { type ComponentPropsWithoutRef } from "react";

interface MetricCardProps extends ComponentPropsWithoutRef<"div"> {
  label: string;
  value: string | number;
  unit?: string;
  valueClassName?: string;
}

export const MetricCard = forwardRef<HTMLDivElement, MetricCardProps>(
  ({ label, value, unit, valueClassName, className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "bg-muted border-border/40 hover:bg-muted/80 hover:border-lazycloud/30 rounded-lg border p-4 transition-colors hover:shadow-sm",
          className,
        )}
        {...props}
      >
        <p className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
          {label}
        </p>
        <p className={cn("text-lazycloud text-2xl font-bold", valueClassName)}>
          {value}
        </p>
        {unit && <p className="text-muted-foreground mt-1 text-xs">{unit}</p>}
      </div>
    );
  },
);
MetricCard.displayName = "MetricCard";

type SectionHeaderProps = ComponentPropsWithoutRef<"p">;

export const SectionHeader = forwardRef<
  HTMLParagraphElement,
  SectionHeaderProps
>(({ className, ...props }, ref) => {
  return (
    <p
      ref={ref}
      className={cn(
        "text-muted-foreground mb-3 text-xs font-semibold tracking-wide uppercase",
        className,
      )}
      {...props}
    />
  );
});
SectionHeader.displayName = "SectionHeader";

interface ServiceCardProps extends ComponentPropsWithoutRef<"div"> {
  name: string;
  cpuHours: number;
  memoryHours: number;
  cost?: {
    cpu: number;
    memory: number;
    total: number;
  };
}

export const ServiceCard = forwardRef<HTMLDivElement, ServiceCardProps>(
  ({ name, cpuHours, memoryHours, cost, className, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "bg-muted border-border/40 hover:bg-muted/80 rounded-lg border p-3 transition-colors",
          className,
        )}
        {...props}
      >
        <div className="mb-2 flex items-center justify-between">
          <span className="text-xs font-semibold">{name}</span>
          {cost && (
            <span className="text-lazycloud text-xs font-bold">
              ${cost.total.toFixed(4)}
            </span>
          )}
        </div>
        <div className="flex gap-3 text-xs">
          <span className="text-muted-foreground">
            <span className="text-[10px] tracking-wide uppercase">CPU</span>{" "}
            <span className="text-foreground font-bold">
              {cpuHours.toFixed(2)}
            </span>
            h
            {cost && (
              <span className="text-muted-foreground/70 ml-1">
                (${cost.cpu.toFixed(4)})
              </span>
            )}
          </span>
          <span className="text-muted-foreground">
            <span className="text-[10px] tracking-wide uppercase">Mem</span>{" "}
            <span className="text-foreground font-bold">
              {memoryHours.toFixed(2)}
            </span>
            h
            {cost && (
              <span className="text-muted-foreground/70 ml-1">
                (${cost.memory.toFixed(4)})
              </span>
            )}
          </span>
        </div>
      </div>
    );
  },
);
ServiceCard.displayName = "ServiceCard";

interface VolumeCardProps extends ComponentPropsWithoutRef<"div"> {
  name: string;
  storageClass: string;
  gbHours: number;
  cost?: number;
}

export const VolumeCard = forwardRef<HTMLDivElement, VolumeCardProps>(
  ({ name, storageClass, gbHours, cost, className, ...props }, ref) => {
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
          <span className="text-xs font-semibold">{name}</span>
          <span className="text-muted-foreground mt-0.5 text-[10px] tracking-wide uppercase">
            {storageClass === "ebs" ? "Standard" : "Shared"}
          </span>
        </div>
        <div className="flex flex-col items-end gap-0.5">
          <span className="text-xs">
            <span className="text-foreground font-bold">
              {gbHours.toFixed(2)}
            </span>
            <span className="text-muted-foreground"> GB-hrs</span>
          </span>
          {cost !== undefined && (
            <span className="text-lazycloud text-xs font-bold">
              ${cost.toFixed(4)}
            </span>
          )}
        </div>
      </div>
    );
  },
);
VolumeCard.displayName = "VolumeCard";

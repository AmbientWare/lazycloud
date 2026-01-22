"use client";

import { cn } from "@/lib/utils";

type StatusType = "running" | "live" | "completed" | "cost";

interface StatusIndicatorProps {
  status: StatusType;
  className?: string;
  showPulse?: boolean;
}

const STATUS_CONFIG: Record<StatusType, { label: string; className: string }> = {
  running: {
    label: "RUNNING",
    className: "bg-green-500/20 text-green-500",
  },
  live: {
    label: "Live",
    className: "bg-green-500/20 text-green-500",
  },
  completed: {
    label: "Completed",
    className: "bg-lazycloud/20 text-lazycloud",
  },
  cost: {
    label: "",
    className: "bg-lazycloud/20 text-lazycloud",
  },
};

export function StatusIndicator({
  status,
  className,
  showPulse = false,
}: StatusIndicatorProps) {
  const config = STATUS_CONFIG[status];

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-semibold",
        config.className,
        className
      )}
    >
      {showPulse && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      )}
      {config.label}
    </span>
  );
}

interface StatusIndicatorWithValueProps {
  value: string;
  className?: string;
}

export function CostIndicator({ value, className }: StatusIndicatorWithValueProps) {
  return (
    <span
      className={cn(
        "rounded-full bg-lazycloud/20 px-2 py-0.5 text-[10px] font-semibold text-lazycloud",
        className
      )}
    >
      {value}
    </span>
  );
}

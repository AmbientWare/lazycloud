"use client";

import { Badge } from "@/components/ui/badge";
import { StyledTooltip } from "./styled-tooltip";
import { cn } from "@/lib/utils";

interface StatusBadgeProps {
  status: "Active" | "Inactive";
  className?: string;
}

export function StatusBadge({ status, className }: StatusBadgeProps) {
  const isActive = status === "Active";

  const tooltipContent = isActive
    ? "This workspace/deployment is currently active and contributing to usage"
    : "This workspace/deployment is inactive but may still contribute to usage during the selected period";

  return (
    <StyledTooltip content={tooltipContent} side="top">
      <Badge
        variant={isActive ? "default" : "secondary"}
        className={cn(
          "w-20 h-6 justify-center",
          isActive
            ? "bg-green-500/10 text-green-600 border-green-500/20 dark:text-green-400 dark:bg-green-500/20"
            : "bg-gray-500/10 text-gray-600 border-gray-500/20 dark:text-gray-400 dark:bg-gray-500/20",
          className,
        )}
      >
        {status}
      </Badge>
    </StyledTooltip>
  );
}


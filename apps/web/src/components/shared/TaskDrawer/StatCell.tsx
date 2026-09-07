import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function StatCell({
  label,
  value,
  className,
}: {
  label: string;
  value: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("px-4 py-2.5", className)}>
      <div className="micro-label">{label}</div>
      <div className="readout mt-1 text-[15px]">{value}</div>
    </div>
  );
}

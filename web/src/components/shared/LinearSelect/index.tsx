import type { ComponentProps, ReactNode } from "react";

import { TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

const frameClassName = "flex min-w-0 shrink-0 items-center gap-2 border-b border-border/80";
const listClassName = "flex h-9 min-w-0 flex-1 items-stretch overflow-x-auto overflow-y-hidden";
const optionClassName =
  "flex h-9 shrink-0 items-center justify-center whitespace-nowrap rounded-none";

export function LinearTabsList({
  ariaLabel,
  action,
  children,
  className,
  listClassName: listClassNameOverride,
}: {
  ariaLabel: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  listClassName?: string;
}) {
  return (
    <div className={cn(frameClassName, className)}>
      <TabsList
        aria-label={ariaLabel}
        className={cn(listClassName, "gap-0 border-b-0", listClassNameOverride)}
      >
        {children}
      </TabsList>
      {action ? <div className="ml-auto shrink-0">{action}</div> : null}
    </div>
  );
}

export function LinearTab({ children, className, ...props }: ComponentProps<typeof TabsTrigger>) {
  return (
    <TabsTrigger className={cn(optionClassName, className)} {...props}>
      {children}
    </TabsTrigger>
  );
}

import * as TabsPrimitive from "@radix-ui/react-tabs";
import type * as React from "react";

import { cn } from "@/lib/utils";

export const Tabs = TabsPrimitive.Root;

export const tabsListClassName = "inline-flex h-8 items-center gap-1 border-b border-border";

export const tabTriggerClassName =
  "-mb-px h-8 rounded-none border-x-0 border-t-0 border-b-2 border-transparent bg-transparent px-3 text-xs font-normal text-muted-foreground outline-none transition-[color,border-color] hover:bg-transparent hover:text-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring data-[state=active]:border-brand data-[state=active]:font-medium data-[state=active]:text-foreground";

export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return <TabsPrimitive.List className={cn(tabsListClassName, className)} {...props} />;
}

export function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      /* Underline tab: selection is carried by the marker and ink weight, never
         by a filled surface, so a tab never reads as a button. */
      className={cn(tabTriggerClassName, className)}
      {...props}
    />
  );
}

export function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return <TabsPrimitive.Content className={cn("outline-none", className)} {...props} />;
}

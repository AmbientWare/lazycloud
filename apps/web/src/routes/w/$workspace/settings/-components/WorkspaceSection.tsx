import type { ReactNode } from "react";

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { cn } from "@/lib/utils";

/**
 * The part of a settings tab that belongs to the workspace you happen to be in.
 *
 * Settings now lead with the account, because a credential, a connected cloud, and a
 * domain belong to a person and read the same in every workspace. What is genuinely
 * per-workspace is collapsed behind this and named with the workspace, so it is never
 * ambiguous which one is about to change.
 *
 * A labelled rule rather than a card: everything it reveals is already framed, and a
 * second frame around framed panels nests one card inside another for no gain.
 */
export function WorkspaceSection({
  workspaceName,
  contentClassName,
  children,
}: {
  workspaceName: string;
  contentClassName?: string;
  children: ReactNode;
}) {
  return (
    <Accordion type="single" collapsible defaultValue="workspace">
      <AccordionItem value="workspace" className="border-b-0">
        <AccordionTrigger className="border-t border-border/80 py-3 hover:no-underline">
          <span className="flex min-w-0 items-baseline gap-2.5 text-left">
            <span className="text-sm font-medium">Workspace</span>
            <span className="mono truncate text-xs font-normal text-muted-foreground">
              {workspaceName}
            </span>
          </span>
        </AccordionTrigger>
        <AccordionContent className={cn("pb-1", contentClassName)}>{children}</AccordionContent>
      </AccordionItem>
    </Accordion>
  );
}

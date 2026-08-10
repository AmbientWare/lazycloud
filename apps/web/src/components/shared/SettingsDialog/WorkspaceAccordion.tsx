import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { WorkspaceDeletionPanel } from "@/components/shared/WorkspaceDeletion/Panel";
import { useWorkspaceDeletion } from "@/components/shared/WorkspaceDeletion/context";
import type { Workspace } from "@/lib/api/schemas";

import { WorkspaceIdentity } from "./WorkspaceIdentity";

/**
 * Every workspace the account belongs to, one section each.
 *
 * Settings is a page about an account, so it lists the account's workspaces rather
 * than showing whichever one the sidebar happens to have selected. A single section
 * that followed the sidebar made a change to "the workspace" ambiguous — you had to
 * look elsewhere to know which one you were about to rename or delete.
 */
export function WorkspaceAccordion({
  workspaces,
  activeWorkspaceName,
}: {
  workspaces: Workspace[];
  activeWorkspaceName: string;
}) {
  const deletion = useWorkspaceDeletion();
  const active = workspaces.find((item) => item.name === activeWorkspaceName);

  if (workspaces.length === 0) {
    return (
      <p className="px-1 py-6 text-center text-sm text-muted-foreground">
        You do not belong to any workspace yet.
      </p>
    );
  }

  return (
    // Collapsed but for the one you are in: a long list of open sections buries the
    // account settings above it, and the active workspace is the likely target.
    <Accordion type="multiple" defaultValue={active ? [active.id] : [workspaces[0].id]}>
      {workspaces.map((workspace) => (
        <AccordionItem key={workspace.id} value={workspace.id} className="border-border/80">
          <AccordionTrigger className="py-3 hover:no-underline">
            <span className="flex min-w-0 items-baseline gap-2.5 text-left">
              <span className="truncate text-sm font-medium">{workspace.name}</span>
              {workspace.name === activeWorkspaceName ? (
                <span className="text-[11px] font-normal text-muted-foreground">current</span>
              ) : null}
            </span>
          </AccordionTrigger>
          <AccordionContent className="grid gap-4 pb-4 lg:grid-cols-5">
            <WorkspaceIdentity workspace={workspace} fullWidth={!deletion.canManage} />
            <WorkspaceDeletionPanel workspace={workspace} />
          </AccordionContent>
        </AccordionItem>
      ))}
    </Accordion>
  );
}

import { useState } from "react";
import { useRouter, useRouterState } from "@tanstack/react-router";
import { Check, ChevronDown, MoreHorizontal, Pencil, Plus, Trash2, Users } from "lucide-react";

import { workspaceLandingPath } from "@/components/shared/AppShell/navigation";
import { CreateWorkspaceSheet } from "@/components/shared/AppShell/CreateWorkspaceSheet";
import { useSession } from "@/components/shared/AuthGate/session";
import { useWorkspaceDeletion } from "@/components/shared/WorkspaceDeletion/context";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { WorkspaceRenameDialog } from "@/components/shared/WorkspaceRename/Dialog";
import type { Workspace } from "@/lib/api/schemas";
import { useWorkspace } from "@/lib/workspace-context";
import { cn } from "@/lib/utils";

import { WorkspaceMembersDialog } from "./WorkspaceMembersDialog";

export function WorkspaceSwitcher({
  className,
  compact = false,
}: {
  className?: string;
  compact?: boolean;
}) {
  const { workspace, workspaces } = useWorkspace();
  const { user } = useSession();
  const [renaming, setRenaming] = useState<Workspace | null>(null);
  const [creating, setCreating] = useState(false);
  const [viewingMembers, setViewingMembers] = useState<Workspace | null>(null);
  const router = useRouter();
  const deletion = useWorkspaceDeletion();
  const path = useRouterState({ select: (state) => state.location.pathname });
  const basePath = `/w/${encodeURIComponent(workspace.name)}`;

  const switchWorkspace = (nextName: string) => {
    if (nextName === workspace.name) return;
    const nextBase = `/w/${encodeURIComponent(nextName)}`;
    router.history.push(workspaceLandingPath(path, basePath, nextBase));
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label="Workspace"
            className={cn(
              // A surface rather than a ghost: this is what every row beneath it
              // is scoped to, and at the same weight as those rows it read as
              // one of them.
              "h-10 w-full justify-between rounded-md border border-sidebar-border bg-sidebar-accent/50 px-2.5 text-sm font-medium text-foreground shadow-none hover:bg-sidebar-accent",
              compact && "h-9 max-w-44 text-xs",
              className,
            )}
          >
            <span className="flex min-w-0 items-center gap-2">
              <span className="truncate">{workspace.name}</span>
              {/* Named rather than shaded. Four statuses collapsed into two dot
                  colours, so a workspace mid-delete looked like a disabled one,
                  and the dot carried it for sighted readers alone. */}
              {workspace.status === "active" ? null : (
                <span className="shrink-0 text-[11px] font-normal text-muted-foreground">
                  {workspace.status}
                </span>
              )}
            </span>
            <ChevronDown className="size-3.5 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-64 p-1.5">
          {/* Creating a workspace is an account-level action rather than one of
              the workspaces below it, so it leads the menu instead of joining
              that list. Administrators only, which is who the platform lets
              create one at all. */}
          {user.role === "administrator" ? (
            <DropdownMenuItem className="min-h-10" onSelect={() => setCreating(true)}>
              <Plus className="text-muted-foreground" />
              Create workspace
            </DropdownMenuItem>
          ) : null}
          <div className="px-2 pb-1.5 pt-1 text-[11px] font-medium text-muted-foreground">
            Workspaces
          </div>
          {workspaces.map((item) => {
            const availability = deletion.availability(item);
            return (
              <div key={item.id} className="flex items-center gap-0.5">
                <DropdownMenuItem
                  className="min-h-10 min-w-0 flex-1"
                  onSelect={() => switchWorkspace(item.name)}
                >
                  <span className="min-w-0 flex-1 truncate">{item.name}</span>
                  {item.status === "active" ? null : (
                    <span className="shrink-0 text-[11px] text-muted-foreground">
                      {item.status}
                    </span>
                  )}
                  {item.id === workspace.id ? (
                    <Check className="text-brand" aria-label="Selected" />
                  ) : null}
                </DropdownMenuItem>
                {/* Renaming is open to anyone who can see the workspace; deleting
                  is not, so the submenu is always offered and only its
                  destructive entry is gated. */}
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger
                    aria-label={`Manage ${item.name} workspace`}
                    className="size-10 justify-center p-0"
                    title={`Manage ${item.name}`}
                  >
                    <MoreHorizontal />
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent>
                    <DropdownMenuItem onSelect={() => setViewingMembers(item)}>
                      <Users />
                      Members
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => setRenaming(item)}>
                      <Pencil />
                      Rename
                    </DropdownMenuItem>
                    {deletion.canManage ? (
                      <DropdownMenuItem
                        variant="destructive"
                        disabled={!availability.allowed}
                        title={availability.allowed ? undefined : availability.reason}
                        onSelect={() => deletion.begin(item, item.id === workspace.id)}
                      >
                        <Trash2 />
                        {item.status === "deleting" ? "Resume deleting" : "Delete"}
                      </DropdownMenuItem>
                    ) : null}
                  </DropdownMenuSubContent>
                </DropdownMenuSub>
              </div>
            );
          })}
        </DropdownMenuContent>
      </DropdownMenu>
      {renaming ? (
        <WorkspaceRenameDialog workspace={renaming} onClose={() => setRenaming(null)} />
      ) : null}
      {creating ? <CreateWorkspaceSheet onClose={() => setCreating(false)} /> : null}
      {viewingMembers ? (
        <WorkspaceMembersDialog
          workspace={viewingMembers}
          onClose={() => setViewingMembers(null)}
        />
      ) : null}
    </>
  );
}

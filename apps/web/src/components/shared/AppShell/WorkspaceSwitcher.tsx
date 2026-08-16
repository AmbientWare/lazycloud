import { useState } from "react";
import { useRouter, useRouterState } from "@tanstack/react-router";
import { Check, ChevronDown, MoreHorizontal, Pencil, Trash2 } from "lucide-react";

import { workspaceLandingPath } from "@/components/shared/AppShell/navigation";
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

export function WorkspaceSwitcher({
  className,
  compact = false,
}: {
  className?: string;
  compact?: boolean;
}) {
  const { workspace, workspaces } = useWorkspace();
  const [renaming, setRenaming] = useState<Workspace | null>(null);
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
              "h-9 w-full justify-between border-transparent px-2 text-xs font-medium text-foreground shadow-none",
              compact && "max-w-44",
              className,
            )}
          >
            <span className="flex min-w-0 items-center gap-2">
              <span
                className={cn(
                  "size-1.5 shrink-0 rounded-full",
                  workspace.status === "active" ? "bg-positive" : "bg-muted-foreground",
                )}
                aria-hidden="true"
              />
              <span className="truncate">{workspace.name}</span>
            </span>
            <ChevronDown className="size-3.5 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-64 p-1.5">
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
                  <span
                    className={cn(
                      "size-1.5 shrink-0 rounded-full",
                      item.status === "active" ? "bg-positive" : "bg-muted-foreground",
                    )}
                    aria-hidden="true"
                  />
                  <span className="min-w-0 flex-1 truncate">{item.name}</span>
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
    </>
  );
}

import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { Loader2, Trash2 } from "lucide-react";

import { useSession } from "@/components/shared/AuthGate/session";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { adminAccessQueryOptions } from "@/lib/queries/compute";
import { currentWorkspaceQueryOptions } from "@/lib/queries/workspace";
import { useWorkspaceSelection } from "@/lib/workspace-selection";

import {
  useWorkspaceDeletionController,
  type WorkspaceDeletionController,
} from "./controller";
import { WorkspaceDeletionContext } from "./context";

export function WorkspaceDeletionProvider({
  children,
}: {
  children: ReactNode;
}) {
  const router = useRouter();
  const { workspaces } = useSession();
  const lastWorkspaceName = useWorkspaceSelection(
    (state) => state.lastWorkspaceName,
  );
  const rememberWorkspaceName = useWorkspaceSelection(
    (state) => state.rememberWorkspaceName,
  );
  const authorityWorkspace =
    workspaces.find((workspace) => workspace.status === "active") ??
    workspaces[0];
  const adminAccess = useQuery(
    adminAccessQueryOptions(authorityWorkspace.id),
  );
  const currentWorkspace = useQuery({
    ...currentWorkspaceQueryOptions(),
    enabled: adminAccess.data === true,
  });
  const controller = useWorkspaceDeletionController({
    canManage: adminAccess.data === true,
    currentWorkspaceId: currentWorkspace.data?.id,
    lastWorkspaceName,
    rememberWorkspaceName,
    replacePath: (path) => router.history.replace(path),
    workspaces,
  });

  return (
    <WorkspaceDeletionContext.Provider value={controller}>
      {children}
      <WorkspaceDeletionDialog controller={controller} />
    </WorkspaceDeletionContext.Provider>
  );
}

function WorkspaceDeletionDialog({
  controller,
}: {
  controller: WorkspaceDeletionController;
}) {
  const workspace = controller.target?.workspace ?? null;
  return (
    <AlertDialog
      open={workspace !== null}
      onOpenChange={(open) => {
        if (!open) controller.close();
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {workspace?.status === "deleting" ? "Resume deleting" : "Delete"}{" "}
            {workspace?.name}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            This permanently removes the workspace identity, access tokens,
            configuration, and remaining owned resources. Enter the workspace
            name to continue.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <label className="block text-xs font-medium text-muted-foreground">
          Workspace name
          <Input
            autoFocus
            value={controller.confirmation}
            onChange={(event) =>
              controller.setConfirmation(event.target.value)
            }
            className="mono mt-1"
            autoComplete="off"
            disabled={controller.isPending}
          />
        </label>
        {controller.error ? (
          <p className="text-sm text-destructive" role="alert">
            {controller.error.message}
          </p>
        ) : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={controller.isPending}>
            Keep workspace
          </AlertDialogCancel>
          <Button
            type="button"
            variant="destructive"
            disabled={
              controller.isPending ||
              workspace === null ||
              controller.confirmation !== workspace.name
            }
            onClick={controller.submit}
          >
            {controller.isPending ? (
              <Loader2 className="animate-spin" />
            ) : (
              <Trash2 />
            )}
            Delete permanently
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

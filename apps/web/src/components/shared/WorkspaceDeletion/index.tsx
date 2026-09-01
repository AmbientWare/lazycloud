import type { ReactNode } from "react";
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
import { useWorkspaceSelection } from "@/lib/workspace-selection";

import { useWorkspaceDeletionController, type WorkspaceDeletionController } from "./controller";
import { WorkspaceDeletionContext } from "./context";

export function WorkspaceDeletionProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { user, workspaces } = useSession();
  const lastWorkspaceName = useWorkspaceSelection((state) => state.lastWorkspaceName);
  const rememberWorkspaceName = useWorkspaceSelection((state) => state.rememberWorkspaceName);
  // The session states the role, so nothing here has to infer it from a 403 against
  // a workspace picked arbitrarily to ask in.
  const canManage = user.role === "administrator";
  const controller = useWorkspaceDeletionController({
    canManage,
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

function WorkspaceDeletionDialog({ controller }: { controller: WorkspaceDeletionController }) {
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
            {workspace?.status === "deleting" ? "Resume deleting" : "Delete"} {workspace?.name}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            This permanently deletes the workspace, its access tokens and configuration, and the
            resources it owns. Enter the workspace name to continue.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <label className="block text-xs font-medium text-muted-foreground">
          Workspace name
          <Input
            autoFocus
            value={controller.confirmation}
            onChange={(event) => controller.setConfirmation(event.target.value)}
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
          <AlertDialogCancel disabled={controller.isPending}>Keep workspace</AlertDialogCancel>
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
            {controller.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}
            Delete permanently
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

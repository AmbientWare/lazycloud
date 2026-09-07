import { useNavigate, useRouter } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { Workspace } from "@/lib/api/schemas";

import { useWorkspaceRenameController } from "./controller";

/** Mounted only while open, so cancelled drafts do not survive reopening. */
export function WorkspaceRenameDialog({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const router = useRouter();
  const rename = useWorkspaceRenameController({
    workspace,
    onRenamed: (name) => {
      const activeWorkspaceName = router.state.matches.find(
        (match) => match.routeId === "/w/$workspace",
      )?.params.workspace;
      if (activeWorkspaceName === workspace.name) {
        void navigate({
          to: ".",
          params: { workspace: name },
          search: true,
          hash: true,
          replace: true,
        });
      }
      onClose();
    },
  });

  return (
    <Dialog open onOpenChange={(next) => (!next && !rename.isSaving ? onClose() : undefined)}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Rename workspace</DialogTitle>
          <DialogDescription>
            Workspace URLs will use the new name. Update anything that uses the current URLs.
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (rename.canSave) rename.save();
          }}
        >
          <Input
            aria-label="Workspace name"
            autoFocus
            disabled={rename.isSaving}
            onChange={(event) => rename.setDraftName(event.target.value)}
            value={rename.draftName}
          />
          {rename.error ? (
            <p className="mt-2 text-xs text-destructive" role="alert">
              {rename.error.message}
            </p>
          ) : null}
          <DialogFooter className="mt-4">
            <Button disabled={rename.isSaving} onClick={onClose} type="button" variant="outline">
              Cancel
            </Button>
            <Button disabled={!rename.canSave} type="submit">
              {rename.isSaving ? <Loader2 className="size-4 animate-spin" /> : null}
              Rename
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

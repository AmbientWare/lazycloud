import { useNavigate } from "@tanstack/react-router";
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

/**
 * Rename one workspace, from wherever it was picked.
 *
 * Mounted only while open, so a cancelled edit leaves no draft and no error
 * behind for the next one. The controller is the same one the settings panel
 * used before this moved to the workspace menu — the rename rules did not
 * change, only where you reach them from.
 */
export function WorkspaceRenameDialog({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const rename = useWorkspaceRenameController({
    workspace,
    // The name is in the address, so a rename has to move the page with it or
    // the next navigation resolves a workspace that no longer answers to it.
    onRenamed: (name) => {
      void navigate({
        to: ".",
        params: { workspace: name },
        search: (previous: Record<string, unknown>) => previous,
        replace: true,
      });
      onClose();
    },
  });

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
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

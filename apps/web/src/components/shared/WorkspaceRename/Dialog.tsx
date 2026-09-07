import { useNavigate } from "@tanstack/react-router";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { FormField } from "@/components/shared/FormField";
import { useWorkspace } from "@/lib/workspace-context";
import type { Workspace } from "@/lib/api/schemas";

import { useWorkspaceRenameController } from "./controller";

export function WorkspaceRenameDialog({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const { workspace: currentWorkspace } = useWorkspace();
  const rename = useWorkspaceRenameController({
    workspace,
    // The name is in the address, so a rename has to move the page with it or
    // the next navigation resolves a workspace that no longer answers to it.
    onRenamed: (name) => {
      if (workspace.id === currentWorkspace.id)
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
    <Dialog open onOpenChange={(next) => (next || rename.isSaving ? undefined : onClose())}>
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
          <FormField
            label="Workspace name"
            autoFocus
            disabled={rename.isSaving}
            onChange={(event) => rename.setDraftName(event.target.value)}
            value={rename.draftName}
            error={rename.error?.message}
          />
          <DialogFooter className="mt-4">
            <Button disabled={rename.isSaving} onClick={onClose} type="button" variant="outline">
              Cancel
            </Button>
            <Button disabled={!rename.canSave} pending={rename.isSaving} type="submit">
              Rename
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

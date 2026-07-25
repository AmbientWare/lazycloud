import { Trash2, TriangleAlert } from "lucide-react";

import { Panel } from "@/components/shared/Panel";
import { Button } from "@/components/ui/button";
import type { Workspace } from "@/lib/api/schemas";

import { useWorkspaceDeletion } from "./context";

export function WorkspaceDeletionPanel({
  workspace,
}: {
  workspace: Workspace;
}) {
  const deletion = useWorkspaceDeletion();
  const availability = deletion.availability(workspace);

  if (!deletion.canManage) return null;

  return (
    <Panel
      title="Delete workspace"
      description="Available only after workspace resources are removed"
      className="min-h-fit lg:col-span-2 lg:col-start-4 lg:row-start-1 lg:min-h-0"
      contentClassName="p-4 lg:overflow-y-auto"
    >
      <div className="flex items-start gap-3">
        <TriangleAlert
          className="mt-0.5 size-4 shrink-0 text-destructive"
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <p className="text-xs leading-5 text-muted-foreground">
            Deletes workspace resources, configuration, and credentials
            permanently. The default, token-owning, and final workspaces are
            protected.
          </p>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            className="mt-3"
            disabled={!availability.allowed}
            title={availability.allowed ? undefined : availability.reason}
            onClick={() => deletion.begin(workspace, true)}
          >
            <Trash2 />
            Delete workspace
          </Button>
        </div>
      </div>
    </Panel>
  );
}

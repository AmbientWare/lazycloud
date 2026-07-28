import { useRouter } from "@tanstack/react-router";
import { Check, Loader2, Pencil } from "lucide-react";

import { CopyId } from "@/components/shared/CopyId";
import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { Workspace } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";

import { useWorkspaceIdentityController } from "./controller";

export function WorkspaceIdentity({
  workspace,
  fullWidth,
}: {
  workspace: Workspace;
  fullWidth: boolean;
}) {
  const router = useRouter();
  const identity = useWorkspaceIdentityController({
    workspace,
    replacePath: (path) => router.history.replace(path),
  });

  return (
    <Panel
      title="Workspace identity"
      action={
        !identity.isEditing ? (
          <Button
            variant="ghost"
            size="icon"
            aria-label="Edit workspace name"
            title="Edit workspace name"
            onClick={identity.beginEditing}
          >
            <Pencil />
          </Button>
        ) : null
      }
      className={`h-fit lg:col-start-1 lg:row-start-1 lg:h-auto lg:min-h-0 ${fullWidth ? "lg:col-span-full" : "lg:col-span-3"}`}
      contentClassName="overflow-visible lg:overflow-y-auto"
    >
      <dl className={`grid grid-cols-2 gap-x-5 gap-y-4 p-4 ${fullWidth ? "sm:grid-cols-4" : ""}`}>
        <div className={identity.isEditing ? "col-span-2 min-w-0" : "min-w-0"}>
          <dt className="micro-label mb-1.5">Name</dt>
          <dd>
            {identity.isEditing ? (
              <form
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  identity.save();
                }}
              >
                <Input
                  autoFocus
                  aria-label="Workspace name"
                  value={identity.draftName}
                  onChange={(event) => identity.setDraftName(event.target.value)}
                  className="mono"
                />
                <div className="flex shrink-0 items-center gap-1">
                  <Button type="submit" size="sm" disabled={!identity.canSave}>
                    {identity.isSaving ? <Loader2 className="animate-spin" /> : <Check />}
                    Save
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={identity.isSaving}
                    onClick={identity.cancel}
                  >
                    Cancel
                  </Button>
                </div>
                {identity.error ? (
                  <p className="col-span-2 text-xs text-destructive" role="alert">
                    {identity.error.message}
                  </p>
                ) : null}
              </form>
            ) : (
              <span className="truncate text-sm font-medium">{workspace.name}</span>
            )}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="micro-label mb-1.5">Workspace ID</dt>
          <dd>
            <CopyId value={workspace.id} className="max-w-full px-0" />
          </dd>
        </div>
        <div>
          <dt className="micro-label mb-1.5">Status</dt>
          <dd>
            <StatusChip status={workspace.status} />
          </dd>
        </div>
        <IdentityFact label="Created" value={relativeTime(workspace.created_at)} />
      </dl>
    </Panel>
  );
}

function IdentityFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="micro-label mb-1.5">{label}</dt>
      <dd className="truncate text-sm font-medium">{value}</dd>
    </div>
  );
}

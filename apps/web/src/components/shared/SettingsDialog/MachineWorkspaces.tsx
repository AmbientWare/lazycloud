import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { UnitMachine, Workspace } from "@/lib/api/schemas";
import { updateMachineWorkspaces } from "@/lib/queries/compute";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";
import { useWorkspace } from "@/lib/workspace-context";

/**
 * Pick which of the account's workspaces a machine serves. Machines carry
 * workspace names, so the selection is by name rather than id.
 */
export function WorkspaceChecklist({
  workspaces,
  selected,
  onChange,
  disabled = false,
}: {
  workspaces: Workspace[];
  selected: ReadonlySet<string>;
  onChange: (next: Set<string>) => void;
  disabled?: boolean;
}) {
  return (
    <ul className="divide-y divide-border border border-border">
      {workspaces.map((workspace) => {
        const checked = selected.has(workspace.name);
        return (
          <li key={workspace.id}>
            <label className="flex cursor-pointer items-center gap-3 px-3 py-2 text-xs has-[:disabled]:cursor-default">
              <Checkbox
                checked={checked}
                disabled={disabled}
                onCheckedChange={(value) => {
                  const next = new Set(selected);
                  if (value === true) next.add(workspace.name);
                  else next.delete(workspace.name);
                  onChange(next);
                }}
              />
              <span className="mono truncate">{workspace.name}</span>
            </label>
          </li>
        );
      })}
    </ul>
  );
}

export function EditMachineWorkspacesDialog({
  machine,
  onOpenChange,
}: {
  machine: UnitMachine | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={machine !== null} onOpenChange={onOpenChange}>
      {machine ? (
        <EditMachineWorkspacesForm machine={machine} onDone={() => onOpenChange(false)} />
      ) : null}
    </Dialog>
  );
}

function EditMachineWorkspacesForm({
  machine,
  onDone,
}: {
  machine: UnitMachine;
  onDone: () => void;
}) {
  const { workspaces } = useWorkspace();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(() => new Set(machine.workspaces));
  const update = useMutation({
    mutationFn: () => updateMachineWorkspaces(machine.id, [...selected]),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: accountQueryKeys.compute.machines() });
      onDone();
    },
  });

  return (
    <DialogContent className="max-w-md">
      <DialogHeader>
        <DialogTitle>Workspaces for {machine.name || machine.id}</DialogTitle>
        <DialogDescription>
          Only workloads from the selected workspaces can run on this machine.
        </DialogDescription>
      </DialogHeader>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (selected.size === 0 || update.isPending) return;
          update.mutate();
        }}
      >
        <WorkspaceChecklist
          workspaces={workspaces}
          selected={selected}
          onChange={setSelected}
          disabled={update.isPending}
        />
        {selected.size === 0 ? (
          <p className="text-xs text-muted-foreground">Select at least one workspace.</p>
        ) : null}
        {update.error ? <p className="text-xs text-destructive">{update.error.message}</p> : null}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onDone} disabled={update.isPending}>
            Cancel
          </Button>
          <Button type="submit" disabled={selected.size === 0 || update.isPending}>
            {update.isPending ? <Loader2 className="animate-spin" /> : null}
            Save
          </Button>
        </div>
      </form>
    </DialogContent>
  );
}

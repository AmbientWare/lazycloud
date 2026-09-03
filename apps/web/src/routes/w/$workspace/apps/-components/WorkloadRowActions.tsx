import { useState, type MouseEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, MoreHorizontal, Trash2 } from "lucide-react";

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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { deleteWorkloadMutationOptions } from "@/lib/queries/apps";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { countLabel } from "@/lib/format";

import type { WorkloadGroup } from "../-workloads/grouping";

/** Deletes every version of one workload from the app's workload list. */
export function WorkloadRowActions({
  group,
  workspaceId,
  appId,
}: {
  group: WorkloadGroup;
  workspaceId: string;
  appId: string;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const queryClient = useQueryClient();
  const deletable = group.deployments.filter((deployment) => deployment.actions.can_delete);
  const remove = useMutation({
    ...deleteWorkloadMutationOptions(
      workspaceId,
      deletable.map((deployment) => deployment.id),
    ),
    onSuccess: async () => {
      setConfirmingDelete(false);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.deployments.root(workspaceId),
        }),
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.apps.detail(workspaceId, appId),
        }),
        queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.apps.summaries(workspaceId) }),
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.containers.root(workspaceId),
        }),
      ]);
    },
  });
  if (deletable.length === 0) {
    return null;
  }
  // The row itself is a link; the menu must not follow it.
  const stop = (event: MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
  };
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-7"
            aria-label={`Open actions for ${group.name}`}
            title="Workload actions"
            onClick={stop}
          >
            <MoreHorizontal className="size-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-48" onClick={stop}>
          <DropdownMenuItem variant="destructive" onSelect={() => setConfirmingDelete(true)}>
            <Trash2 />
            Delete workload
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <AlertDialog open={confirmingDelete} onOpenChange={setConfirmingDelete}>
        <AlertDialogContent onClick={stop}>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {group.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              This stops its containers and deletes {countLabel(deletable.length, "version")} of
              this workload. The app stays.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {remove.error ? <p className="text-sm text-destructive">{remove.error.message}</p> : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={remove.isPending}>Cancel</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              {remove.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}
              Delete workload
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

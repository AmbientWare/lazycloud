import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Trash2 } from "lucide-react";

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

import type { WorkloadSummary } from "@/lib/api/schemas";

/** Deletes every version of one workload from the app's workload list. */
export function WorkloadRowActions({
  workload,
  workspaceId,
  appId,
}: {
  workload: WorkloadSummary;
  workspaceId: string;
  appId: string;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const queryClient = useQueryClient();
  const deployment = workload.deployment;
  const remove = useMutation({
    ...deleteWorkloadMutationOptions(workspaceId, appId, deployment.name),
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
  if (!deployment.actions.can_delete) {
    return null;
  }
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-7"
            aria-label={`Open actions for ${deployment.name}`}
            title="Workload actions"
          >
            <MoreHorizontal className="size-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-48">
          <DropdownMenuItem variant="destructive" onSelect={() => setConfirmingDelete(true)}>
            <Trash2 />
            Delete workload
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <AlertDialog open={confirmingDelete} onOpenChange={setConfirmingDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {deployment.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              This stops its containers and deletes {countLabel(workload.version_count, "version")}{" "}
              of this workload. The app stays.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {remove.error ? <p className="text-sm text-destructive">{remove.error.message}</p> : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={remove.isPending}>Cancel</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              pending={remove.isPending}
              onClick={() => remove.mutate()}
            >
              <Trash2 />
              Delete workload
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

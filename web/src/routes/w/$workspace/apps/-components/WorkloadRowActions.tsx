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
import { invalidateAppLists } from "@/lib/queries/apps";
import { deleteDeployment, type DeployedWorkload } from "@/lib/queries/deployments";

/** Deletes one workload, every version of it, from the app's workload list. */
export function WorkloadRowActions({
  workload,
  workspace,
}: {
  workload: DeployedWorkload;
  workspace: string;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => deleteDeployment(workspace, workload.id),
    onSuccess: async () => {
      setConfirmingDelete(false);
      await invalidateAppLists(queryClient, workspace);
    },
  });
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
            aria-label={`Open actions for ${workload.name}`}
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
            <AlertDialogTitle>Delete {workload.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              This cancels its queued and running tasks, stops its containers and deletes every
              version of this workload. The app stays.
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

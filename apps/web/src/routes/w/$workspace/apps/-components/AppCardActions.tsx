import { useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Trash2 } from "lucide-react";

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
import type { App } from "@/lib/api/schemas";
import { deleteAppMutationOptions } from "@/lib/queries/apps";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

export function AppCardActions({
  app,
  workspaceId,
  actionIcon,
  defaultOpen = false,
}: {
  app: App;
  workspaceId: string;
  actionIcon: ReactNode;
  defaultOpen?: boolean;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const queryClient = useQueryClient();
  const remove = useMutation({
    ...deleteAppMutationOptions(workspaceId, app.id),
    onSuccess: async () => {
      setConfirmingDelete(false);
      queryClient.removeQueries({
        queryKey: workspaceQueryKeys.apps.detail(workspaceId, app.id),
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.apps.summaries(workspaceId) }),
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.deployments.root(workspaceId),
        }),
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.containers.root(workspaceId),
        }),
      ]);
    },
  });

  return (
    <>
      <DropdownMenu defaultOpen={defaultOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-7 bg-card/85"
            aria-label={`Open actions for ${app.name}`}
            title="App actions"
          >
            {actionIcon}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-44">
          {app.actions.can_delete ? (
            <DropdownMenuItem variant="destructive" onSelect={() => setConfirmingDelete(true)}>
              <Trash2 />
              Delete app
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>

      <AlertDialog open={confirmingDelete} onOpenChange={setConfirmingDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {app.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              This stops its deployments and containers and removes the app from this workspace.
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {remove.error ? (
            <p className="text-sm text-destructive" role="alert">
              {remove.error.message}
            </p>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={remove.isPending}>Keep app</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              {remove.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}
              Delete app
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

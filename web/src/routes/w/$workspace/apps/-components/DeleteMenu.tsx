import { useState, type MouseEvent } from "react";
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
import { invalidateAppLists } from "@/lib/queries/apps";

/** An actions menu whose one item deletes an app or a workload after confirming. */
export function DeleteMenu({
  workspace,
  name,
  noun,
  description,
  cancelLabel,
  remove: removeResource,
  triggerClassName,
  iconClassName,
  menuClassName,
  insideLink = false,
}: {
  workspace: string;
  name: string;
  noun: "app" | "workload";
  description: string;
  cancelLabel: string;
  remove: () => Promise<unknown>;
  triggerClassName: string;
  iconClassName?: string;
  menuClassName: string;
  /** The menu sits inside a link and must not follow it. */
  insideLink?: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: removeResource,
    onSuccess: async () => {
      setConfirming(false);
      await invalidateAppLists(queryClient, workspace);
    },
  });
  const stop = insideLink
    ? (event: MouseEvent) => {
        event.preventDefault();
        event.stopPropagation();
      }
    : undefined;
  const label = `Delete ${noun}`;
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className={triggerClassName}
            aria-label={`Open actions for ${name}`}
            title={noun === "app" ? "App actions" : "Workload actions"}
            onClick={stop}
          >
            <MoreHorizontal className={iconClassName} />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={menuClassName} onClick={stop}>
          <DropdownMenuItem variant="destructive" onSelect={() => setConfirming(true)}>
            <Trash2 />
            {label}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent onClick={stop}>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {name}?</AlertDialogTitle>
            <AlertDialogDescription>{description}</AlertDialogDescription>
          </AlertDialogHeader>
          {remove.error ? (
            <p className="text-sm text-destructive" role="alert">
              {remove.error.message}
            </p>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={remove.isPending}>{cancelLabel}</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              pending={remove.isPending}
              icon={<Trash2 />}
              onClick={() => remove.mutate()}
            >
              {label}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

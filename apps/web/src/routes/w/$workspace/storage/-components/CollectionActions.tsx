import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { deleteCollection, refreshCollection } from "@/lib/queries/collections";

export function ConfirmCollectionAction({
  label,
  description,
  action,
  disabled = false,
}: {
  label: string;
  description: string;
  action: () => Promise<void>;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [confirmedAction, setConfirmedAction] = useState<(() => Promise<void>) | null>(null);
  const mutation = useMutation({
    mutationFn: async () => {
      if (!confirmedAction) throw new Error("Choose an action before confirming.");
      await confirmedAction();
    },
    onSuccess: () => setOpen(false),
  });
  return (
    <>
      <Button
        variant="outline"
        size="sm"
        disabled={disabled}
        onClick={() => {
          mutation.reset();
          setConfirmedAction(() => action);
          setOpen(true);
        }}
      >
        {label}
      </Button>
      <Dialog
        open={open}
        onOpenChange={(value) => {
          if (!mutation.isPending) setOpen(value);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{label}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>
          {mutation.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {mutation.error.message}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" disabled={mutation.isPending} onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate()}
            >
              {mutation.isPending ? "Applying…" : label}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function DeleteCollection({
  workspaceId,
  kind,
  name,
}: {
  workspaceId: string;
  kind: "maps" | "queues";
  name: string;
}) {
  const client = useQueryClient();
  return (
    <ConfirmCollectionAction
      label={kind === "maps" ? "Delete map" : "Delete queue"}
      description={`Delete "${name}" and all its ${kind === "maps" ? "keys" : "remaining messages"}? This cannot be undone. Running code can recreate it.`}
      action={async () => {
        await deleteCollection(workspaceId, kind, name);
        await refreshCollection(client, workspaceId, kind, name);
      }}
    />
  );
}

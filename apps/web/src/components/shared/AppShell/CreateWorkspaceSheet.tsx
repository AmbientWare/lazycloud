import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";

import { DrawerHeader } from "@/components/shared/DrawerHeader";
import { Button } from "@/components/ui/button";
import { FormField } from "@/components/shared/FormField";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import { createWorkspace } from "@/lib/queries/workspace";

export function CreateWorkspaceSheet({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => createWorkspace(name.trim()),
    onSuccess: async (created) => {
      // The shell resolves a workspace out of the session, so the session has to
      // know about the new one before the route changes to it.
      await queryClient.invalidateQueries({ queryKey: currentSessionQueryOptions().queryKey });
      onClose();
      router.history.push(`/w/${encodeURIComponent(created.name)}/apps`);
    },
  });

  return (
    <Sheet open onOpenChange={(next) => (next || create.isPending ? undefined : onClose())}>
      <SheetContent aria-describedby={undefined} className="gap-0 sm:max-w-md">
        <DrawerHeader>
          <SheetTitle>Create workspace</SheetTitle>
        </DrawerHeader>
        <form
          className="flex flex-col gap-3 p-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!create.isPending && name.trim()) create.mutate();
          }}
        >
          <FormField
            label="Name"
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="workspace-name"
            className="mono"
            disabled={create.isPending}
            error={create.error?.message}
          />
          <div className="flex items-center gap-2">
            <Button type="submit" size="sm" pending={create.isPending} disabled={!name.trim()}>
              Create
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onClose}
              disabled={create.isPending}
            >
              Cancel
            </Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}

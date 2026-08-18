import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { DrawerHeader } from "@/components/shared/DrawerHeader";
import { Button } from "@/components/ui/button";
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
    <Sheet open onOpenChange={(next) => (next ? undefined : onClose())}>
      <SheetContent aria-describedby={undefined} className="gap-0 sm:max-w-md">
        <DrawerHeader>
          <SheetTitle>Create workspace</SheetTitle>
        </DrawerHeader>
        <form
          className="flex flex-col gap-3 p-4"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <label className="block text-xs font-medium text-muted-foreground">
            Name
            <input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="workspace-name"
              className="mono mt-1 h-9 w-full rounded-md border border-input bg-muted px-3 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
          <div className="flex items-center gap-2">
            <Button type="submit" size="sm" disabled={create.isPending || !name.trim()}>
              {create.isPending ? <Loader2 className="size-3.5 animate-spin" /> : "Create"}
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={onClose}>
              Cancel
            </Button>
          </div>
          {create.isError ? (
            <p className="text-xs text-destructive">{create.error.message}</p>
          ) : null}
        </form>
      </SheetContent>
    </Sheet>
  );
}

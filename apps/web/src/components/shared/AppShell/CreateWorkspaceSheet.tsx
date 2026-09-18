import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { DrawerHeader } from "@/components/shared/DrawerHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import { awsConnectionQueryOptions } from "@/lib/queries/compute";
import { createWorkspace } from "@/lib/queries/workspace";
import { cn } from "@/lib/utils";

const CREATE_WORKSPACE_MUTATION_KEY = ["workspaces", "create"];

type Location = "lazycloud" | "aws";

export function CreateWorkspaceSheet({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [location, setLocation] = useState<Location>("lazycloud");
  const connection = useQuery(awsConnectionQueryOptions());
  const readyConnection =
    connection.data && connection.data.phase === "ready" ? connection.data : null;
  const create = useMutation({
    mutationKey: CREATE_WORKSPACE_MUTATION_KEY,
    mutationFn: () =>
      createWorkspace(
        name.trim(),
        location === "aws" && readyConnection ? readyConnection.id : null,
      ),
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
            if (
              !name.trim() ||
              queryClient.isMutating({ mutationKey: CREATE_WORKSPACE_MUTATION_KEY, exact: true })
            )
              return;
            create.mutate();
          }}
        >
          <label className="block text-xs font-medium text-muted-foreground">
            Name
            <Input
              autoFocus
              disabled={create.isPending}
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="workspace-name"
              className="mono mt-1"
            />
          </label>
          {readyConnection ? (
            <fieldset className="flex flex-col gap-1.5" disabled={create.isPending}>
              <legend className="text-xs font-medium text-muted-foreground">Location</legend>
              <LocationOption
                selected={location === "lazycloud"}
                onSelect={() => setLocation("lazycloud")}
                title="LazyCloud"
                detail="Compute and volumes on LazyCloud."
              />
              <LocationOption
                selected={location === "aws"}
                onSelect={() => setLocation("aws")}
                title={`AWS account ${readyConnection.account_id}`}
                detail="Compute and volumes in your connected account. This cannot be changed later."
              />
            </fieldset>
          ) : null}
          <div className="flex items-center gap-2">
            <Button type="submit" size="sm" disabled={create.isPending || !name.trim()}>
              {create.isPending ? <Loader2 className="size-3.5 animate-spin" /> : null}
              Create
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={create.isPending}
              onClick={onClose}
            >
              Cancel
            </Button>
          </div>
          {create.isError ? (
            <p role="alert" className="text-xs text-destructive">
              {create.error.message}
            </p>
          ) : null}
        </form>
      </SheetContent>
    </Sheet>
  );
}

function LocationOption({
  selected,
  onSelect,
  title,
  detail,
}: {
  selected: boolean;
  onSelect: () => void;
  title: string;
  detail: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onSelect}
      className={cn(
        "rounded-md border px-3 py-2 text-left text-xs transition-colors",
        selected ? "border-foreground/40 bg-muted" : "border-border hover:bg-muted/50",
      )}
    >
      <span className="block font-medium">{title}</span>
      <span className="block text-muted-foreground">{detail}</span>
    </button>
  );
}

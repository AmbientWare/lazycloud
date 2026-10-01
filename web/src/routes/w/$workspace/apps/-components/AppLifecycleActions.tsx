import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Loader2, Pause, Play, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { Schemas } from "@/lib/api/client";
import {
  deleteAppMutationOptions,
  invalidateAppLists,
  pauseAppMutationOptions,
  resumeAppMutationOptions,
} from "@/lib/queries/apps";

export function AppLifecycleActions({
  app,
  workspace,
}: {
  app: Schemas["App"];
  workspace: string;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const refresh = () => invalidateAppLists(queryClient, workspace);
  const pause = useMutation({
    ...pauseAppMutationOptions(workspace, app.name),
    onSuccess: refresh,
  });
  const resume = useMutation({
    ...resumeAppMutationOptions(workspace, app.name),
    onSuccess: refresh,
  });
  const remove = useMutation({
    ...deleteAppMutationOptions(workspace, app.name),
    onSuccess: async () => {
      await navigate({ to: "/w/$workspace/apps", params: { workspace } });
      await invalidateAppLists(queryClient, workspace);
    },
  });
  const pending = pause.isPending || resume.isPending || remove.isPending;
  const error = pause.error ?? resume.error ?? remove.error;
  if (app.state === "deleted") return null;

  return (
    <div className="flex min-w-0 flex-wrap items-center justify-end gap-1">
      {confirmingDelete ? (
        <>
          <span className="px-1 text-xs text-muted-foreground">Delete app?</span>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            disabled={pending}
            onClick={() => remove.mutate()}
          >
            {remove.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}
            Delete
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={pending}
            onClick={() => setConfirmingDelete(false)}
          >
            Keep
          </Button>
        </>
      ) : (
        <>
          {app.state === "active" ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={pending}
              onClick={() => pause.mutate()}
            >
              {pause.isPending ? <Loader2 className="animate-spin" /> : <Pause />}
              Pause
            </Button>
          ) : null}
          {app.state === "paused" ? (
            <Button
              type="button"
              variant="default"
              size="sm"
              disabled={pending}
              onClick={() => resume.mutate()}
            >
              {resume.isPending ? <Loader2 className="animate-spin" /> : <Play />}
              Resume
            </Button>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Delete app ${app.name}`}
            title="Delete app"
            disabled={pending}
            onClick={() => setConfirmingDelete(true)}
          >
            <Trash2 className="text-destructive" />
          </Button>
        </>
      )}
      {error ? (
        <span className="basis-full text-right text-xs text-destructive" role="alert">
          {error.message}
        </span>
      ) : null}
    </div>
  );
}

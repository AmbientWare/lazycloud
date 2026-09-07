import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Loader2, Pause, Play, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { App } from "@/lib/api/schemas";
import {
  deleteAppMutationOptions,
  pauseAppMutationOptions,
  resumeAppMutationOptions,
} from "@/lib/queries/apps";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

export function AppLifecycleActions({
  app,
  workspaceId,
  workspaceName,
}: {
  app: App;
  workspaceId: string;
  workspaceName: string;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.apps.detail(workspaceId, app.id),
      }),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.apps.summaries(workspaceId) }),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.deployments.root(workspaceId) }),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.containers.root(workspaceId) }),
    ]);
  };
  const pause = useMutation({
    ...pauseAppMutationOptions(workspaceId, app.id),
    onSuccess: refresh,
  });
  const resume = useMutation({
    ...resumeAppMutationOptions(workspaceId, app.id),
    onSuccess: refresh,
  });
  const remove = useMutation({
    ...deleteAppMutationOptions(workspaceId, app.id),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: workspaceQueryKeys.apps.detail(workspaceId, app.id) });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.apps.summaries(workspaceId) }),
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.deployments.root(workspaceId),
        }),
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.containers.root(workspaceId),
        }),
      ]);
      await navigate({ to: "/w/$workspace/apps", params: { workspace: workspaceName } });
    },
  });
  const pending = pause.isPending || resume.isPending || remove.isPending;
  const error = pause.error ?? resume.error ?? remove.error;
  const hasActions = app.actions.can_pause || app.actions.can_resume || app.actions.can_delete;

  if (!hasActions) return null;

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
          {app.actions.can_pause ? (
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
          {app.actions.can_resume ? (
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
          {app.actions.can_delete ? (
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
          ) : null}
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

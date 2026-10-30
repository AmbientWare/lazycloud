import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Loader2, Pause, Play, Trash2 } from "lucide-react";

import { ConfirmButton } from "@/components/shared/ConfirmButton";
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
      <ConfirmButton
        label={`Delete app ${app.name}`}
        icon={Trash2}
        disabled={pause.isPending || resume.isPending}
        pending={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
      {error ? (
        <span className="basis-full text-right text-xs text-destructive" role="alert">
          {error.message}
        </span>
      ) : null}
    </div>
  );
}

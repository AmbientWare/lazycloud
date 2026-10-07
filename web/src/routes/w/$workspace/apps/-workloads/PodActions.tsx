import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Play, Square } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { Schemas } from "@/lib/api/client";
import { invalidateAppLists } from "@/lib/queries/apps";
import {
  startWorkloadMutationOptions,
  stopWorkloadMutationOptions,
  workloadRunning,
} from "@/lib/queries/deployments";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

/**
 * Stop and Start for a pod's header. Stopping the deployment stops its
 * containers whatever they are doing: waiting for a machine, starting, or
 * failing to start and retrying.
 */
export function PodActions({
  workspace,
  workload,
}: {
  workspace: string;
  workload: Schemas["Workload"];
}) {
  const queryClient = useQueryClient();
  const refresh = () =>
    Promise.all([
      invalidateAppLists(queryClient, workspace),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.containers.root(workspace) }),
    ]);
  const start = useMutation({
    ...startWorkloadMutationOptions(workspace, workload),
    onSuccess: refresh,
  });
  const stop = useMutation({
    ...stopWorkloadMutationOptions(workspace, workload),
    onSuccess: refresh,
  });
  const running = workloadRunning(workload);
  // A paused app starts from its own page.
  if (!running && workload.app_state === "paused") return null;
  const pending = start.isPending || stop.isPending;
  const error = start.error ?? stop.error;

  return (
    <>
      {error ? (
        <p className="text-xs text-destructive" role="alert">
          {error.message}
        </p>
      ) : null}
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="min-w-26"
        disabled={pending}
        onClick={() => {
          start.reset();
          stop.reset();
          (running ? stop : start).mutate();
        }}
      >
        {pending ? (
          <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
        ) : running ? (
          <Square className="fill-current" />
        ) : (
          <Play />
        )}
        {running ? "Stop" : "Start"}
      </Button>
    </>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Square } from "lucide-react";

import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  killSandboxProcessMutationOptions,
  sandboxProcessesQueryOptions,
} from "@/lib/queries/sandboxes";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

export function SandboxProcessList({
  containerId,
  writable,
  className,
}: {
  containerId: string;
  writable: boolean;
  className?: string;
}) {
  const { workspace } = useWorkspace();
  const queryClient = useQueryClient();
  const query = useQuery(sandboxProcessesQueryOptions(workspace.id, containerId));
  const kill = useMutation({
    ...killSandboxProcessMutationOptions(workspace.id, containerId),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.sandboxes.processes(workspace.id, containerId),
      }),
  });

  return (
    <div className={cn("flex min-h-0 flex-col overflow-hidden", className)}>
      <div className="grid grid-cols-[4rem_1fr_2rem] border-b border-border px-3 py-2 text-[11px] uppercase text-muted-foreground">
        <span>PID</span>
        <span>Command</span>
        <span className="sr-only">Actions</span>
      </div>
      {kill.isError ? (
        <p className="border-b border-border px-3 py-2 text-xs text-destructive">
          {kill.error.message}
        </p>
      ) : null}
      <div className="min-h-0 flex-1 divide-y divide-border/60 overflow-y-auto">
        {query.isPending ? (
          <div aria-hidden="true">
            {Array.from({ length: 3 }, (_, index) => (
              <div key={index} className="flex items-center gap-4 px-3 py-2">
                <Skeleton className="h-3.5 w-10" />
                <Skeleton className="h-3.5 w-56" />
              </div>
            ))}
          </div>
        ) : query.isError ? (
          <PanelError message={query.error.message} />
        ) : query.data.processes.length === 0 ? (
          <PanelEmpty message="No processes" className="p-4" />
        ) : (
          query.data.processes.map((process) => (
            <div
              key={process.pid}
              className="grid min-h-9 grid-cols-[4rem_1fr_2rem] items-center px-3 text-xs"
            >
              <span className="mono tabular-nums text-muted-foreground">{process.pid}</span>
              <span className="mono truncate text-foreground" title={process.command}>
                {process.command}
              </span>
              {writable && process.pid > 1 ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="size-7"
                  aria-label={`Stop process ${process.pid}`}
                  title="Stop process"
                  disabled={kill.isPending}
                  onClick={() => kill.mutate(process.pid)}
                >
                  {kill.isPending && kill.variables === process.pid ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <Square className="fill-current" />
                  )}
                </Button>
              ) : (
                <span />
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

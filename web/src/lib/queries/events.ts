import { queryOptions } from "@tanstack/react-query";

import { ApiError, api, ok, type Schemas } from "@/lib/api/client";
import type { ContainerEventSummary } from "@/lib/api/schemas";
import { workspaceName } from "@/lib/api/workspaces";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/*
 * A container's start as the reference's lifecycle summary. The API records
 * the wait for a host, each start stage and the drain from durable
 * timestamps; a stage changes with the container's state, so the change
 * stream refreshes these and nothing polls them.
 */

/** Stages between placement and readiness: what the timeline calls preparation. */
const PREPARATION: ReadonlySet<Schemas["LifecycleStageKind"]> = new Set([
  "image",
  "source",
  "create",
  "runtime",
]);

function viewLifecycle(lifecycle: Schemas["ContainerLifecycle"]): ContainerEventSummary {
  const summary: Record<string, number> = {};
  for (const stage of lifecycle.stages) summary[stage.stage] = stage.duration_ms ?? 0;
  return {
    container_id: lifecycle.container_id,
    task_id: null,
    stub_id: null,
    event_count: lifecycle.stages.length,
    summary,
    lifecycle: lifecycle.stages
      .filter((stage) => PREPARATION.has(stage.stage))
      .map((stage) => ({
        event_id: stage.stage,
        duration_ms: stage.duration_ms ?? 0,
        start_time: stage.started_at,
        end_time: stage.finished_at ?? null,
      })),
    missing: [],
  };
}

export function callGraphLifecycleQueryOptions(
  workspaceId: string,
  rootTaskId: string,
  containerIds: string[],
  live: boolean,
) {
  const ids = [...new Set(containerIds)].sort();
  return queryOptions({
    queryKey: [...workspaceQueryKeys.tasks.callGraph(workspaceId, rootTaskId), "lifecycle", ids],
    queryFn: async (): Promise<{ count: number; items: ContainerEventSummary[] }> => {
      const workspace = workspaceName(workspaceId);
      // The API takes up to 200 containers a call.
      const batches: string[][] = [];
      for (let index = 0; index < ids.length; index += 200) {
        batches.push(ids.slice(index, index + 200));
      }
      const pages = await Promise.all(
        batches.map((container_ids) =>
          ok(
            api.POST("/v1/workspaces/{workspace}/containers/lifecycles", {
              params: { path: { workspace } },
              body: { container_ids },
            }),
          ),
        ),
      );
      const items = pages.flatMap((page) => page.lifecycles.map(viewLifecycle));
      return { count: items.length, items };
    },
    enabled: ids.length > 0,
    meta: workspaceLiveQueryMeta(false, live),
  });
}

export function containerEventSummaryQueryOptions(workspaceId: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.eventSummary(workspaceId, containerId),
    queryFn: async (): Promise<ContainerEventSummary | null> => {
      try {
        const lifecycle = await ok(
          api.GET("/v1/workspaces/{workspace}/containers/{container}/lifecycle", {
            params: { path: { workspace: workspaceName(workspaceId), container: containerId } },
          }),
        );
        return viewLifecycle(lifecycle);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    meta: workspaceLiveQueryMeta(false),
  });
}

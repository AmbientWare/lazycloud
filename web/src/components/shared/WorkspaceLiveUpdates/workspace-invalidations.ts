import type { QueryKey } from "@tanstack/react-query";

import type { Schemas } from "@/lib/api/client";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

export type WorkspaceInvalidationTarget = {
  queryKey: QueryKey;
  expensive?: boolean;
};

/**
 * The cached reads one committed change can have moved. A grouped change
 * (`count` without `resource_id`) names no resource, so it refreshes every
 * detail of its topic.
 */
export function workspaceInvalidationTargets(
  workspace: string,
  change: Schemas["ResourceChange"],
): WorkspaceInvalidationTarget[] {
  const keys = workspaceQueryKeys;
  const grouped = change.resource_id === undefined;
  const taskId = change.task_id ?? (change.topic === "tasks" ? change.resource_id : undefined);
  const rootTaskId = change.root_task_id ?? taskId;
  const containerId =
    change.container_id ?? (change.topic === "containers" ? change.resource_id : undefined);
  const appSummaries = { queryKey: keys.apps.summaries(workspace), expensive: true };

  switch (change.topic) {
    // A change names its app by id and app reads are keyed by name, so an app
    // or deployment change, which is rare, refreshes every app read.
    case "apps":
      return [{ queryKey: keys.apps.root(workspace) }];
    case "deployments":
      return [
        { queryKey: keys.workloads.root(workspace) },
        { queryKey: keys.apps.root(workspace) },
      ];
    case "tasks":
      return compact([
        { queryKey: keys.tasks.lists(workspace) },
        grouped
          ? { queryKey: keys.tasks.details(workspace) }
          : taskId && { queryKey: keys.tasks.detail(workspace, taskId) },
        grouped
          ? { queryKey: keys.tasks.callGraphs(workspace) }
          : rootTaskId && { queryKey: keys.tasks.callGraph(workspace, rootTaskId) },
        containerId && { queryKey: keys.containers.detail(workspace, containerId) },
        containerId && { queryKey: keys.containers.lifecycle(workspace, containerId) },
        { queryKey: keys.tasks.aggregates(workspace), expensive: true },
        appSummaries,
        { queryKey: keys.apps.activities(workspace), expensive: true },
      ]);
    case "containers":
      return compact([
        { queryKey: keys.containers.lists(workspace) },
        grouped
          ? { queryKey: keys.containers.details(workspace) }
          : containerId && { queryKey: keys.containers.detail(workspace, containerId) },
        containerId && { queryKey: keys.containers.lifecycle(workspace, containerId) },
        taskId && { queryKey: keys.tasks.detail(workspace, taskId) },
        // A container's start and stop move its tasks' timelines.
        { queryKey: keys.tasks.callGraphs(workspace) },
        appSummaries,
      ]);
    case "storage.secrets":
      return [{ queryKey: keys.storage.secrets(workspace) }];
  }
}

function compact(
  targets: Array<WorkspaceInvalidationTarget | "" | undefined>,
): WorkspaceInvalidationTarget[] {
  return targets.filter((target): target is WorkspaceInvalidationTarget => Boolean(target));
}

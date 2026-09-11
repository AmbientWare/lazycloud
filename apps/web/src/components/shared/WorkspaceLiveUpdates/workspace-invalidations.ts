import type { QueryKey } from "@tanstack/react-query";

import type { WorkspaceChangeEvent } from "@/lib/api/schemas";
import { accountQueryKeys, workspaceQueryKeys } from "@/lib/queries/workspace-keys";

export type WorkspaceInvalidationTarget = {
  queryKey: QueryKey;
  expensive?: boolean;
};

export function workspaceInvalidationTargets(
  workspaceId: string,
  event: WorkspaceChangeEvent,
): WorkspaceInvalidationTarget[] {
  const appId = event.app_id ?? (event.topic === "apps" ? event.resource_id : null);
  const taskId = event.task_id ?? (event.topic === "tasks" ? event.resource_id : null);
  const rootTaskId = event.root_task_id ?? taskId;
  const containerId =
    event.container_id ?? (event.topic === "containers" ? event.resource_id : null);

  switch (event.topic) {
    case "apps":
      return compactTargets([
        { queryKey: workspaceQueryKeys.apps.summaries(workspaceId), expensive: true },
        appId ? { queryKey: workspaceQueryKeys.apps.detail(workspaceId, appId) } : null,
        { queryKey: workspaceQueryKeys.tasks.details(workspaceId) },
        { queryKey: workspaceQueryKeys.containers.details(workspaceId) },
      ]);
    case "deployments":
      return compactTargets([
        { queryKey: workspaceQueryKeys.deployments.root(workspaceId) },
        { queryKey: workspaceQueryKeys.workloads.root(workspaceId) },
        { queryKey: workspaceQueryKeys.apps.summaries(workspaceId), expensive: true },
        appId ? { queryKey: workspaceQueryKeys.apps.detail(workspaceId, appId) } : null,
        { queryKey: workspaceQueryKeys.tasks.details(workspaceId) },
        { queryKey: workspaceQueryKeys.containers.details(workspaceId) },
      ]);
    case "workloads":
      return compactTargets([
        { queryKey: workspaceQueryKeys.workloads.root(workspaceId) },
        { queryKey: workspaceQueryKeys.deployments.root(workspaceId) },
        { queryKey: workspaceQueryKeys.sandboxes.root(workspaceId) },
        { queryKey: workspaceQueryKeys.apps.summaries(workspaceId), expensive: true },
        appId ? { queryKey: workspaceQueryKeys.apps.detail(workspaceId, appId) } : null,
        { queryKey: workspaceQueryKeys.tasks.details(workspaceId) },
        { queryKey: workspaceQueryKeys.containers.details(workspaceId) },
      ]);
    case "tasks":
      return compactTargets([
        { queryKey: workspaceQueryKeys.tasks.lists(workspaceId) },
        taskId ? { queryKey: workspaceQueryKeys.tasks.detail(workspaceId, taskId) } : null,
        rootTaskId
          ? { queryKey: workspaceQueryKeys.tasks.callGraph(workspaceId, rootTaskId) }
          : null,
        containerId
          ? { queryKey: workspaceQueryKeys.containers.detail(workspaceId, containerId) }
          : null,
        containerId
          ? { queryKey: workspaceQueryKeys.containers.eventSummary(workspaceId, containerId) }
          : null,
        { queryKey: workspaceQueryKeys.tasks.aggregates(workspaceId), expensive: true },
        { queryKey: workspaceQueryKeys.apps.summaries(workspaceId), expensive: true },
      ]);
    case "containers":
      return compactTargets([
        { queryKey: workspaceQueryKeys.tasks.lists(workspaceId) },
        { queryKey: workspaceQueryKeys.tasks.details(workspaceId) },
        { queryKey: workspaceQueryKeys.containers.lists(workspaceId) },
        containerId
          ? { queryKey: workspaceQueryKeys.containers.detail(workspaceId, containerId) }
          : null,
        containerId
          ? { queryKey: workspaceQueryKeys.containers.eventSummary(workspaceId, containerId) }
          : null,
        taskId ? { queryKey: workspaceQueryKeys.tasks.detail(workspaceId, taskId) } : null,
        { queryKey: workspaceQueryKeys.sandboxes.root(workspaceId) },
        { queryKey: workspaceQueryKeys.apps.summaries(workspaceId), expensive: true },
      ]);
    // Capacity belongs to the account, so the change one workspace's stream
    // reports is a change to what every workspace of that account reads.
    case "compute.units":
      return [{ queryKey: accountQueryKeys.compute.instances() }];
    case "compute.machines":
    case "compute.workers":
      return [
        { queryKey: accountQueryKeys.compute.machines() },
        { queryKey: accountQueryKeys.compute.instances() },
      ];
    case "compute.agents":
    case "compute.providers":
      return [];
    case "compute.connections":
      return [
        { queryKey: accountQueryKeys.compute.awsConnection() },
        { queryKey: accountQueryKeys.compute.instances() },
      ];
    case "storage.secrets":
      return [{ queryKey: workspaceQueryKeys.storage.secrets(workspaceId) }];
    case "storage.volumes":
      return compactTargets([
        { queryKey: workspaceQueryKeys.storage.volumes(workspaceId) },
        event.change === "deleted"
          ? { queryKey: accountQueryKeys.usage.root(), expensive: true }
          : null,
      ]);
    case "usage":
      return [{ queryKey: accountQueryKeys.usage.root(), expensive: true }];
    case "settings.concurrency":
      return [];
  }
}

function compactTargets(
  targets: Array<WorkspaceInvalidationTarget | null>,
): WorkspaceInvalidationTarget[] {
  return targets.filter((target): target is WorkspaceInvalidationTarget => target !== null);
}

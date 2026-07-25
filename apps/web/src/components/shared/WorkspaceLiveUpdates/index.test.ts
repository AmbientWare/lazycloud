import { describe, expect, it } from "vitest";

import type { WorkspaceChangeEvent } from "@/lib/api/schemas";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { workspaceInvalidationTargets } from "./workspace-invalidations";

function event(
  topic: WorkspaceChangeEvent["topic"],
  fields: Partial<WorkspaceChangeEvent> = {},
): WorkspaceChangeEvent {
  return {
    event_id: "1710000000000-0",
    occurred_at: "2026-07-13T15:30:00Z",
    workspace_id: "workspace-1",
    topic,
    change: "updated",
    resource_id: "resource-1",
    ...fields,
  };
}

describe("workspace live invalidation ownership", () => {
  it("refreshes task graph, aggregates, and joined container context", () => {
    const targets = workspaceInvalidationTargets(
      "workspace-1",
      event("tasks", {
        task_id: "task-2",
        root_task_id: "task-1",
        container_id: "container-1",
      }),
    );

    expect(targets).toEqual([
      { queryKey: workspaceQueryKeys.tasks.lists("workspace-1") },
      { queryKey: workspaceQueryKeys.tasks.detail("workspace-1", "task-2") },
      { queryKey: workspaceQueryKeys.tasks.callGraph("workspace-1", "task-1") },
      { queryKey: workspaceQueryKeys.containers.detail("workspace-1", "container-1") },
      { queryKey: workspaceQueryKeys.containers.eventSummary("workspace-1", "container-1") },
      { queryKey: workspaceQueryKeys.tasks.aggregates("workspace-1"), expensive: true },
      { queryKey: workspaceQueryKeys.apps.summaries("workspace-1"), expensive: true },
    ]);
  });

  it("refreshes lifecycle and workload projections without container metrics", () => {
    const containerTargets = workspaceInvalidationTargets(
      "workspace-1",
      event("containers", { task_id: "task-1", container_id: "container-1" }),
    );
    expect(containerTargets).toContainEqual({
      queryKey: workspaceQueryKeys.tasks.detail("workspace-1", "task-1"),
    });
    expect(JSON.stringify(containerTargets)).not.toContain("metrics");

    const workloadTargets = workspaceInvalidationTargets(
      "workspace-1",
      event("workloads", { app_id: "app-1", stub_id: "stub-1" }),
    );
    expect(workloadTargets).toContainEqual({
      queryKey: workspaceQueryKeys.apps.detail("workspace-1", "app-1"),
    });
    expect(workloadTargets).toContainEqual({
      queryKey: workspaceQueryKeys.sandboxes.root("workspace-1"),
    });
  });
});

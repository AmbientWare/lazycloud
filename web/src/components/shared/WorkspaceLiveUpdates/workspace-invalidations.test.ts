import { describe, expect, it } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { workspaceInvalidationTargets } from "./workspace-invalidations";

function change(
  topic: Schemas["ChangeTopic"],
  fields: Partial<Schemas["ResourceChange"]> = {},
): Schemas["ResourceChange"] {
  return { topic, change: "updated", resource_id: "resource-1", ...fields };
}

describe("workspace live invalidation ownership", () => {
  it("refreshes task graph, aggregates, and joined container context", () => {
    const targets = workspaceInvalidationTargets(
      "dev",
      change("tasks", {
        resource_id: "task-2",
        task_id: "task-2",
        root_task_id: "task-1",
        container_id: "container-1",
      }),
    );

    expect(targets).toEqual([
      { queryKey: workspaceQueryKeys.tasks.lists("dev") },
      { queryKey: workspaceQueryKeys.tasks.detail("dev", "task-2") },
      { queryKey: workspaceQueryKeys.tasks.callGraph("dev", "task-1") },
      { queryKey: workspaceQueryKeys.containers.detail("dev", "container-1") },
      { queryKey: workspaceQueryKeys.containers.lifecycle("dev", "container-1") },
      { queryKey: workspaceQueryKeys.tasks.aggregates("dev"), expensive: true },
      { queryKey: workspaceQueryKeys.apps.summaries("dev"), expensive: true },
    ]);
  });

  it("refreshes every detail of a grouped change and no container metrics", () => {
    const grouped = workspaceInvalidationTargets(
      "dev",
      change("tasks", { resource_id: undefined, count: 40 }),
    );
    expect(grouped).toContainEqual({ queryKey: workspaceQueryKeys.tasks.details("dev") });
    expect(grouped).toContainEqual({ queryKey: workspaceQueryKeys.tasks.callGraphs("dev") });

    const containerTargets = workspaceInvalidationTargets(
      "dev",
      change("containers", { resource_id: "container-1", task_id: "task-1" }),
    );
    expect(containerTargets).toContainEqual({
      queryKey: workspaceQueryKeys.tasks.detail("dev", "task-1"),
    });
    expect(JSON.stringify(containerTargets)).not.toContain("metrics");
  });
});

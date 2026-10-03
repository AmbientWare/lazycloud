import { describe, expect, it } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { accountQueryKeys, workspaceQueryKeys } from "@/lib/queries/workspace-keys";

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
        deployment_id: "workload-1",
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
      { queryKey: workspaceQueryKeys.apps.activities("dev"), expensive: true },
      { queryKey: workspaceQueryKeys.workloads.activity("dev", "workload-1"), expensive: true },
    ]);
  });

  it("refreshes request lists and the serving workload's activity once requests are recorded", () => {
    const recorded = { resource_id: undefined, count: 3 };
    expect(
      workspaceInvalidationTargets(
        "dev",
        change("requests", { ...recorded, deployment_id: "workload-1" }),
      ),
    ).toEqual([
      { queryKey: workspaceQueryKeys.requests.lists("dev") },
      { queryKey: workspaceQueryKeys.workloads.activity("dev", "workload-1"), expensive: true },
    ]);
    expect(workspaceInvalidationTargets("dev", change("requests", recorded))).toContainEqual({
      queryKey: workspaceQueryKeys.workloads.activities("dev"),
      expensive: true,
    });
  });

  it("refreshes running counts on a container change, and activity on a cold start", () => {
    const stopped = workspaceInvalidationTargets(
      "dev",
      change("containers", { container_id: "container-1", deployment_id: "workload-1" }),
    );
    for (const queryKey of [
      workspaceQueryKeys.workloads.lists("dev"),
      workspaceQueryKeys.workloads.details("dev"),
      workspaceQueryKeys.apps.details("dev"),
    ]) {
      expect(stopped).toContainEqual({ queryKey });
    }
    const activity = {
      queryKey: workspaceQueryKeys.workloads.activity("dev", "workload-1"),
      expensive: true,
    };
    expect(stopped).not.toContainEqual(activity);
    expect(
      workspaceInvalidationTargets(
        "dev",
        change("containers", { change: "created", deployment_id: "workload-1" }),
      ),
    ).toContainEqual(activity);
  });

  it("ignores a topic newer than the page", () => {
    const future = change("future" as Schemas["ChangeTopic"]);
    expect(workspaceInvalidationTargets("dev", future)).toEqual([]);
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

  it("refreshes the volume list, and usage once charges or a deletion land", () => {
    const usage = { queryKey: accountQueryKeys.usage.root(), expensive: true };
    expect(workspaceInvalidationTargets("dev", change("storage.volumes"))).toEqual([
      { queryKey: workspaceQueryKeys.storage.volumes("dev") },
    ]);
    expect(
      workspaceInvalidationTargets("dev", change("storage.volumes", { change: "deleted" })),
    ).toEqual([{ queryKey: workspaceQueryKeys.storage.volumes("dev") }, usage]);
    expect(
      workspaceInvalidationTargets("dev", change("usage", { resource_id: undefined, count: 3 })),
    ).toEqual([usage]);
  });
});

import { describe, expect, it } from "vitest";

import type { Deployment, DeploymentList } from "@/lib/api/schemas";

import { nextDeploymentCursor, selectDeploymentList } from "./deployments";

describe("deployment pagination", () => {
  it("projects every deployment page without truncating app history", () => {
    const deployments = Array.from({ length: 205 }, (_, index) =>
      deployment(`deployment-${index}`, index + 1),
    );
    const pages = [
      deploymentPage(deployments.slice(0, 100), "cursor-100"),
      deploymentPage(deployments.slice(100, 200), "cursor-200"),
      deploymentPage(deployments.slice(200), ""),
    ];

    const selected = selectDeploymentList({ pages }, false);

    expect(selected.items).toHaveLength(205);
    expect(selected.items.map((item) => item.id)).toEqual(deployments.map((item) => item.id));
    expect(selected.nextCursor).toBeUndefined();
  });

  it("deduplicates refreshed page overlap and stops repeated cursors", () => {
    const first = deploymentPage([deployment("deployment-2", 2)], "cursor-2");
    const repeated = deploymentPage(
      [deployment("deployment-2", 2), deployment("deployment-1", 1)],
      "cursor-2",
    );

    expect(
      selectDeploymentList({ pages: [first, repeated] }, true).items.map((item) => item.id),
    ).toEqual(["deployment-2", "deployment-1"]);
    expect(nextDeploymentCursor(first, [first])).toBe("cursor-2");
    expect(nextDeploymentCursor(repeated, [first, repeated])).toBeUndefined();
  });
});

function deployment(id: string, version: number): Deployment {
  return {
    id,
    name: "worker",
    kind: "function",
    app_id: "app-1",
    stub_id: `stub-${version}`,
    version,
    spec: {
      resources: { gpu_count: 0, concurrency: 1 },
      methods: [],
      command: [],
      ports: {},
      placement: null,
    },
    active: true,
    created_at: "2026-07-10T12:00:00Z",
    updated_at: "2026-07-10T12:00:00Z",
    actions: { can_start: false, can_stop: true, can_delete: true, can_scale: false },
  };
}

function deploymentPage(data: Deployment[], next: string): DeploymentList {
  return { data, next };
}

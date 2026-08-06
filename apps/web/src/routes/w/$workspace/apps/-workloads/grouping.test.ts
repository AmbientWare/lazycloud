import { describe, expect, it } from "vitest";

import type { Deployment } from "@/lib/api/schemas";

import { currentDeployment, findWorkloadGroup, groupDeploymentsByWorkload } from "./grouping";

function deployment(overrides: Partial<Deployment>): Deployment {
  return {
    id: "deployment-1",
    name: "square",
    kind: "function",
    app_id: "app-1",
    stub_id: "stub-1",
    version: 1,
    spec: {
      resources: { gpu_count: 0, concurrency: 1 },
      methods: [],
      command: [],
      ports: {},
      pool: "lazycloud",
    },
    active: false,
    actions: { can_start: false, can_stop: false, can_delete: false, can_scale: false },
    created_at: "2026-01-01T10:00:00Z",
    updated_at: "2026-01-01T10:00:00Z",
    ...overrides,
  };
}

describe("groupDeploymentsByWorkload", () => {
  it("folds versions of one name into a single group, newest first", () => {
    const groups = groupDeploymentsByWorkload(
      [
        deployment({ id: "d1", version: 1, stub_id: "s1" }),
        deployment({ id: "d3", version: 3, stub_id: "s3", active: true }),
        deployment({ id: "d2", version: 2, stub_id: "s2" }),
      ],
      "app-1",
    );

    expect(groups).toHaveLength(1);
    const group = groups[0];
    expect(group.name).toBe("square");
    expect(group.deployments.map((item) => item.version)).toEqual([3, 2, 1]);
    expect(group.latest.id).toBe("d3");
    expect(group.active).toBe(true);
    expect(group.stubIds).toEqual(["s3", "s2", "s1"]);
  });

  it("scopes to the requested app and skips other apps", () => {
    const groups = groupDeploymentsByWorkload(
      [
        deployment({ id: "d1", app_id: "app-1" }),
        deployment({ id: "d2", app_id: "app-2", name: "other" }),
      ],
      "app-1",
    );
    expect(groups.map((group) => group.name)).toEqual(["square"]);
  });

  it("orders groups by most recent deployment", () => {
    const groups = groupDeploymentsByWorkload(
      [
        deployment({ id: "d1", name: "old-fn", created_at: "2026-01-01T08:00:00Z" }),
        deployment({ id: "d2", name: "new-fn", created_at: "2026-01-02T08:00:00Z" }),
      ],
      "app-1",
    );
    expect(groups.map((group) => group.name)).toEqual(["new-fn", "old-fn"]);
  });

  it("marks workloads with a stopped newest version inactive", () => {
    const groups = groupDeploymentsByWorkload(
      [
        deployment({ id: "d1", version: 1 }),
        deployment({ id: "d2", version: 2, kind: "endpoint" }),
      ],
      "app-1",
    );
    const group = groups[0];
    expect(group.active).toBe(false);
    expect(group.kind).toBe("endpoint");
    expect(currentDeployment(group).id).toBe("d2");
  });

  it("routes to the newest version even when an older version is still active", () => {
    const groups = groupDeploymentsByWorkload(
      [deployment({ id: "d1", version: 1, active: true }), deployment({ id: "d2", version: 2 })],
      "app-1",
    );
    expect(groups[0].active).toBe(false);
    expect(currentDeployment(groups[0]).id).toBe("d2");
  });

  it("finds a group by workload name", () => {
    const rows = [deployment({ id: "d1" })];
    expect(findWorkloadGroup(rows, "app-1", "square")?.latest.id).toBe("d1");
    expect(findWorkloadGroup(rows, "app-1", "missing")).toBeUndefined();
  });
});

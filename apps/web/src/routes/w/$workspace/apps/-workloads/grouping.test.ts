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
      resources: {
        gpu: [],
        gpu_count: 0,
        concurrency: 1,
        availability_zone: "",
        preemptible: false,
      },
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

  it("routes to the newest version even when an older version is still active", () => {
    const groups = groupDeploymentsByWorkload(
      [deployment({ id: "d1", version: 1, active: true }), deployment({ id: "d2", version: 2 })],
      "app-1",
    );
    expect(groups[0].active).toBe(false);
    expect(currentDeployment(groups[0]).id).toBe("d2");
  });

  it("isolates same-name workloads by kind for selection, versions, and actions", () => {
    const rows = [
      deployment({ id: "f7", version: 7, stub_id: "function-7", active: true }),
      deployment({ id: "e1", kind: "endpoint", stub_id: "endpoint-1" }),
      deployment({ id: "f6", version: 6, stub_id: "function-6" }),
      deployment({ id: "e2", kind: "endpoint", version: 2, stub_id: "endpoint-2" }),
      deployment({ id: "other-app", app_id: "app-2", version: 8 }),
    ];

    expect(groupDeploymentsByWorkload(rows, "app-1")).toHaveLength(2);
    const fn = findWorkloadGroup(rows, "app-1", "function", "square");
    const endpoint = findWorkloadGroup(rows, "app-1", "endpoint", "square");
    expect(fn?.latest.id).toBe("f7");
    expect(fn?.active).toBe(true);
    expect(fn?.deployments.map((item) => item.id)).toEqual(["f7", "f6"]);
    expect(fn?.stubIds).toEqual(["function-7", "function-6"]);
    expect(endpoint?.latest.id).toBe("e2");
    expect(endpoint?.active).toBe(false);
    expect(endpoint?.deployments.map((item) => item.id)).toEqual(["e2", "e1"]);
    expect(endpoint?.stubIds).toEqual(["endpoint-2", "endpoint-1"]);
    expect(findWorkloadGroup(rows, "app-1", "pod", "square")).toBeUndefined();
  });
});

import { describe, expect, it } from "vitest";

import { workspaceChangeEventSchema } from "./events";

const validEvent = {
  event_id: "1710000000000-0",
  occurred_at: "2026-07-13T15:30:00Z",
  workspace_id: "workspace-1",
  topic: "tasks",
  change: "updated",
  resource_id: "task-1",
  app_id: "app-1",
  deployment_id: "deployment-1",
  stub_id: "stub-1",
  task_id: "task-1",
  root_task_id: "task-root",
  container_id: "container-1",
} as const;

describe("workspaceChangeEventSchema", () => {
  it("accepts the typed workspace change identity envelope", () => {
    expect(workspaceChangeEventSchema.parse(validEvent)).toEqual(validEvent);
  });

  it("rejects telemetry payloads and unsupported topics", () => {
    expect(() =>
      workspaceChangeEventSchema.parse({ ...validEvent, data: { cpu_percent: 50 } }),
    ).toThrow();
    expect(() =>
      workspaceChangeEventSchema.parse({ ...validEvent, topic: "container.metrics" }),
    ).toThrow();
  });
});

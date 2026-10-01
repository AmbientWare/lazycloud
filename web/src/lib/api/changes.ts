import type { Schemas } from "@/lib/api/client";
import type { WorkspaceChangeEvent, WorkspaceChangeTopic } from "@/lib/api/schemas";

/*
 * The API's change stream as the reference's `workspace.change` frames. One
 * API event carries every change a statement committed; each becomes one
 * reference event under the API event's id, so a reconnect resumes after the
 * whole statement.
 */

type Frame = { id: string; event: string; data: string };

const TOPICS: Record<Schemas["ChangeTopic"], WorkspaceChangeTopic> = {
  apps: "apps",
  deployments: "deployments",
  tasks: "tasks",
  containers: "containers",
  "storage.secrets": "storage.secrets",
};

/**
 * A reset means the server could not deliver some changes, so everything the
 * dashboard shows is reloaded: one change per topic, which between them
 * invalidate every live list, detail and aggregate.
 */
const RESET_TOPICS: readonly WorkspaceChangeTopic[] = [
  "apps",
  "workloads",
  "tasks",
  "containers",
  "storage.secrets",
];

export function viewChangeFrames(frame: Frame, workspaceId: string): Frame[] {
  if (frame.event === "reset") {
    const occurredAt = new Date().toISOString();
    return RESET_TOPICS.map((topic) =>
      referenceFrame(frame.id, {
        event_id: frame.id || "reset",
        occurred_at: occurredAt,
        workspace_id: workspaceId,
        topic,
        change: "updated",
        resource_id: workspaceId,
      }),
    );
  }
  if (frame.event !== "change") return [];
  const event = JSON.parse(frame.data) as Schemas["ChangeEvent"];
  return event.changes.flatMap((change) => {
    const resource = change.resource_id ?? change.app_id ?? event.workspace_id;
    const view: WorkspaceChangeEvent = {
      event_id: String(event.seq),
      occurred_at: event.occurred_at,
      workspace_id: event.workspace_id,
      topic: TOPICS[change.topic],
      change: change.change,
      resource_id: resource,
      app_id: change.app_id ?? null,
      deployment_id: change.deployment_id ?? null,
      task_id: change.task_id ?? null,
      root_task_id: change.root_task_id ?? null,
      container_id: change.container_id ?? null,
    };
    // A grouped change names no resource, so the details it may have changed
    // are refreshed through the workload topic, which reaches all of them.
    const grouped =
      change.resource_id === undefined ? [{ ...view, topic: "workloads" as const }] : [];
    return [view, ...grouped].map((item) => referenceFrame(frame.id, item));
  });
}

function referenceFrame(id: string, event: WorkspaceChangeEvent): Frame {
  return { id, event: "workspace.change", data: JSON.stringify(event) };
}

import type { Workspace } from "../../../src/lib/api/schemas";

export const activeWorkspaceDefaults = {
  status: "active",
  signing_key_prefix: null,
  primary_token_id: null,
  concurrency_limit_id: null,
  storage: { backend: "local", bucket: null, prefix: "" },
  labels: {},
  metadata: {},
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
} as const satisfies Omit<Workspace, "id" | "name">;

import { describe, expect, it } from "vitest";

import type { Workspace } from "@/lib/api/schemas";

import { workspaceDeleteAvailability } from "@/lib/workspace-deletion";

const workspace = (id: string, name: string): Workspace => ({
  id,
  name,
  status: "active",
  signing_key_prefix: null,
  primary_token_id: null,
  concurrency_limit_id: null,
  storage: { backend: "local", bucket: null, prefix: "" },
  labels: {},
  metadata: {},
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});

describe("workspaceDeleteAvailability", () => {
  it("protects the default and token-owning workspaces", () => {
    expect(workspaceDeleteAvailability(workspace("one", "default"), "two", 2)).toEqual({
      allowed: false,
      reason: "The default workspace is protected",
    });
    expect(workspaceDeleteAvailability(workspace("one", "acme"), "one", 2)).toEqual({
      allowed: false,
      reason: "The workspace that owns this admin token is protected",
    });
  });

  it("waits for the protection owner and preserves the final workspace", () => {
    expect(workspaceDeleteAvailability(workspace("one", "acme"), undefined, 2)).toEqual({
      allowed: false,
      reason: "Checking workspace protection",
    });
    expect(workspaceDeleteAvailability(workspace("one", "acme"), "two", 1)).toEqual({
      allowed: false,
      reason: "The final workspace is protected",
    });
  });

  it("allows an admin to remove another non-default workspace", () => {
    expect(workspaceDeleteAvailability(workspace("two", "beta"), "one", 2)).toEqual({
      allowed: true,
    });
  });
});

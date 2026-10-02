import { describe, expect, it } from "vitest";

import type { Schemas } from "@/lib/api/client";

type Workspace = Schemas["Workspace"];

import { workspaceDeleteAvailability } from "@/lib/workspace-deletion";

const workspace = (id: string, name: string): Workspace => ({
  id,
  name,
  state: "active",
  role: "owner",
  created_at: "2026-01-01T00:00:00Z",
});

describe("workspaceDeleteAvailability", () => {
  it("protects the default workspace and the last remaining one", () => {
    expect(workspaceDeleteAvailability(workspace("one", "default"), 2)).toEqual({
      allowed: false,
      reason: "The default workspace is protected",
    });
    expect(workspaceDeleteAvailability(workspace("one", "acme"), 1)).toEqual({
      allowed: false,
      reason: "The final workspace is protected",
    });
  });

  it("offers deletion for another non-default workspace", () => {
    expect(workspaceDeleteAvailability(workspace("two", "beta"), 2)).toEqual({
      allowed: true,
    });
  });
});

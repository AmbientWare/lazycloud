import { describe, expect, it } from "vitest";

import type { DeploymentList } from "@/lib/api/schemas";

import { nextDeploymentCursor } from "./deployments";

describe("nextDeploymentCursor", () => {
  it("stops paging when the API hands back a cursor it already served", () => {
    // Deployment lists continue automatically inside their scroll region, so a
    // repeated cursor would fetch the same page forever against the API.
    const first: DeploymentList = { data: [], next: "cursor-2" };
    const repeated: DeploymentList = { data: [], next: "cursor-2" };

    expect(nextDeploymentCursor(first, [first])).toBe("cursor-2");
    expect(nextDeploymentCursor(repeated, [first, repeated])).toBeUndefined();
  });
});

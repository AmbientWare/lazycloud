import { describe, expect, it, vi } from "vitest";

import { rememberWorkspaces } from "@/lib/api/workspaces";
import { testQueryClient } from "@/test/query-client";

import { callGraphQueryOptions } from "./tasks";

const WORKSPACE_ID = "0199a000-0000-7000-8000-000000000001";

function node(task_id: string, parent_task_id?: string, status = "succeeded") {
  return {
    task_id,
    parent_task_id,
    app: "demo",
    function: `fn-${task_id}`,
    status,
    created_at: "2026-10-01T12:00:00Z",
    depends_on: [],
  };
}

describe("call graph", () => {
  it("nests the API's flat, oldest-first nodes under the task that spawned each", async () => {
    rememberWorkspaces([{ id: WORKSPACE_ID, name: "dev" }]);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          root_task_id: "a",
          truncated: false,
          nodes: [node("a"), node("b", "a"), node("c", "b", "queued"), node("d", "a")],
        }),
      ),
    );

    const graph = await testQueryClient().fetchQuery(callGraphQueryOptions(WORKSPACE_ID, "c"));

    expect(graph.root?.task_id).toBe("a");
    expect(graph.nodes).toHaveLength(1);
    const [root] = graph.nodes;
    expect(root.children.map((child) => child.task_id)).toEqual(["b", "d"]);
    expect(root.children[0].children.map((child) => [child.task_id, child.status])).toEqual([
      ["c", "pending"],
    ]);
    expect(root.status).toBe("complete");
  });
});

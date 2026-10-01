import { InfiniteQueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { testQueryClient } from "@/test/query-client";

import { callGraphQueryOptions, tasksInfiniteQueryOptions } from "./tasks";

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

    const roots = await testQueryClient().fetchQuery(callGraphQueryOptions("dev", "a"));

    expect(roots.map((root) => root.task_id)).toEqual(["a"]);
    const [root] = roots;
    expect(root.children.map((child) => child.task_id)).toEqual(["b", "d"]);
    expect(root.children[0].children.map((child) => [child.task_id, child.status])).toEqual([
      ["c", "queued"],
    ]);
  });
});

describe("task lists", () => {
  it("narrows on the server and shows every row of each page it returns", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const cursor = new URL((input as Request).url).searchParams.get("cursor");
      return Response.json({
        tasks: [{ id: cursor ? "older" : "newest", parent_task_id: cursor ? "newest" : undefined }],
        next_cursor: cursor ? undefined : "page-2",
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const observer = new InfiniteQueryObserver(
      testQueryClient(),
      tasksInfiniteQueryOptions("dev", {
        app: "shop",
        function: "checkout",
        version: 3,
        status: "failed",
        root_only: true,
        search: "0199",
      }),
    );
    try {
      await observer.refetch();
      const result = await observer.fetchNextPage();

      const query = new URL((fetchMock.mock.calls[0]?.[0] as Request).url).searchParams;
      expect(Object.fromEntries(query)).toEqual({
        app: "shop",
        function: "checkout",
        version: "3",
        status: "failed",
        root_only: "true",
        search: "0199",
        limit: "50",
      });
      // The server answered for the filter, so a child it returns is not dropped here.
      expect(result.data?.pages.flatMap((page) => page.data.map((task) => task.id))).toEqual([
        "newest",
        "older",
      ]);
    } finally {
      observer.destroy();
    }
  });
});

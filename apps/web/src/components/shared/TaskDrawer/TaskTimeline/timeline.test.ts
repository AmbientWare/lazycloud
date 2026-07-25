import { describe, expect, it } from "vitest";

import type { CallGraphNode } from "@/lib/api/schemas";

import { flattenCallGraph, rowSegments, timelineDomain } from "./timeline";

function node(overrides: Partial<CallGraphNode> & { task_id: string }): CallGraphNode {
  return {
    parent_task_id: "",
    root_task_id: "",
    status: "complete",
    name: overrides.task_id,
    function_name: "",
    created_at: null,
    started_at: null,
    finished_at: null,
    dependencies: [],
    children: [],
    ...overrides,
  };
}

const T0 = "2026-07-01T00:00:00Z";
const T10 = "2026-07-01T00:00:10Z";
const T20 = "2026-07-01T00:00:20Z";
const T40 = "2026-07-01T00:00:40Z";
const domain = { startMs: Date.parse(T0), endMs: Date.parse(T40) };

describe("task call graph timeline projection", () => {
  it("flattens the graph depth-first with connector ownership", () => {
    const rows = flattenCallGraph([
      node({
        task_id: "root",
        children: [
          node({ task_id: "child-a", children: [node({ task_id: "grandchild" })] }),
          node({ task_id: "child-b" }),
        ],
      }),
    ]);

    expect(rows.map((row) => [row.node.task_id, row.depth, row.isLastSibling])).toEqual([
      ["root", 0, true],
      ["child-a", 1, false],
      ["grandchild", 2, true],
      ["child-b", 1, true],
    ]);
  });

  it("derives completed and unfinished timeline domains", () => {
    const completed = flattenCallGraph([
      node({ task_id: "a", created_at: T0, started_at: T10, finished_at: T20 }),
      node({ task_id: "b", created_at: T10, started_at: T20, finished_at: T40 }),
    ]);
    const running = flattenCallGraph([
      node({ task_id: "running", status: "running", created_at: T0, started_at: T10 }),
    ]);

    expect(timelineDomain(completed, Date.parse(T40))).toEqual(domain);
    expect(timelineDomain(running, Date.parse(T40))).toEqual(domain);
  });

  it.each([
    {
      name: "completed task",
      task: node({ task_id: "complete", created_at: T0, started_at: T10, finished_at: T20 }),
      now: T40,
      expected: [
        ["queued", 0, 25, 10_000],
        ["run", 25, 25, 10_000],
      ],
    },
    {
      name: "running task",
      task: node({ task_id: "running", status: "running", created_at: T0, started_at: T10 }),
      now: T40,
      expected: [
        ["queued", 0, 25, 10_000],
        ["run", 25, 75, 30_000],
      ],
    },
    {
      name: "pending task",
      task: node({ task_id: "pending", status: "pending", created_at: T10 }),
      now: T20,
      expected: [["queued", 25, 25, 10_000]],
    },
  ])("projects $name segments in the shared domain", ({ task, now, expected }) => {
    const segments = rowSegments(task, domain, Date.parse(now));
    expect(
      segments.map((segment) => [
        segment.kind,
        segment.leftPct,
        segment.widthPct,
        segment.durationMs,
      ]),
    ).toEqual(expected);
  });
});

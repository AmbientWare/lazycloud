import { describe, expect, it } from "vitest";

import { nextPageCursor, selectPages } from "./infinite-list";

it("stops paging when the API repeats a cursor", () => {
  const first = { next_cursor: "cursor-2" };
  const repeated = { next_cursor: "cursor-2" };

  expect(nextPageCursor(first, [first])).toBe("cursor-2");
  expect(nextPageCursor(repeated, [first, repeated])).toBeUndefined();
  expect(nextPageCursor({}, [first, {}])).toBeUndefined();
});

describe("selectPages", () => {
  it("exposes items and the active continuation cursor without page plumbing", () => {
    const selection = selectPages(
      {
        pages: [
          { tasks: [{ id: "run-3" }, { id: "run-2" }], next_cursor: "cursor-2" },
          { tasks: [{ id: "run-1" }], next_cursor: "cursor-1" },
        ],
      },
      (page) => page.tasks,
      true,
      (item) => item.id,
    );

    expect(selection).toEqual({
      items: [{ id: "run-3" }, { id: "run-2" }, { id: "run-1" }],
      nextCursor: "cursor-1",
    });
  });

  it("deduplicates overlapping pages by the domain identity key", () => {
    const selection = selectPages(
      {
        pages: [
          { containers: [{ id: "container-2" }], next_cursor: "cursor-2" },
          { containers: [{ id: "container-2" }, { id: "container-1" }] },
        ],
      },
      (page) => page.containers,
      false,
      (item) => item.id,
    );

    expect(selection.items.map((item) => item.id)).toEqual(["container-2", "container-1"]);
    expect(selection.nextCursor).toBeUndefined();
  });
});

import { describe, expect, it } from "vitest";

import { nextListCursor, selectInfiniteList } from "./infinite-list";

it("stops paging when the API repeats a cursor", () => {
  const first = { next: "cursor-2" };
  const repeated = { next: "cursor-2" };

  expect(nextListCursor(first, [first])).toBe("cursor-2");
  expect(nextListCursor(repeated, [first, repeated])).toBeUndefined();
});

describe("selectInfiniteList", () => {
  it("exposes items and the active continuation cursor without page plumbing", () => {
    const selection = selectInfiniteList(
      {
        pages: [
          { data: [{ id: "run-3" }, { id: "run-2" }], next: "cursor-2" },
          { data: [{ id: "run-1" }], next: "cursor-1" },
        ],
      },
      true,
      (item) => item.id,
    );

    expect(selection).toEqual({
      items: [{ id: "run-3" }, { id: "run-2" }, { id: "run-1" }],
      nextCursor: "cursor-1",
    });
  });

  it("deduplicates overlapping pages by the domain identity key", () => {
    const selection = selectInfiniteList(
      {
        pages: [
          { data: [{ id: "container-2" }], next: "cursor-2" },
          { data: [{ id: "container-2" }, { id: "container-1" }], next: "" },
        ],
      },
      false,
      (item) => item.id,
    );

    expect(selection.items.map((item) => item.id)).toEqual(["container-2", "container-1"]);
    expect(selection.nextCursor).toBeUndefined();
  });
});

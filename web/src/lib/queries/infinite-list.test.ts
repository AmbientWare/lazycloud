import { expect, it } from "vitest";

import { nextListCursor, selectInfiniteList } from "./infinite-list";

it("stops paging when the API repeats a cursor", () => {
  const first = { next: "cursor-2" };
  const repeated = { next: "cursor-2" };

  expect(nextListCursor(first, [first])).toBe("cursor-2");
  expect(nextListCursor(repeated, [first, repeated])).toBeUndefined();
});

it("lists overlapping pages once each by identity and keeps the last page's cursor", () => {
  const pages = [
    { data: [{ id: "run-3" }, { id: "run-2" }], next: "cursor-2" },
    { data: [{ id: "run-2" }, { id: "run-1" }], next: "cursor-1" },
  ];

  expect(selectInfiniteList({ pages }, true, (item) => item.id)).toEqual({
    items: [{ id: "run-3" }, { id: "run-2" }, { id: "run-1" }],
    nextCursor: "cursor-1",
  });
  expect(selectInfiniteList({ pages }, false, (item) => item.id).nextCursor).toBeUndefined();
});

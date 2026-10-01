import { testQueryClient } from "@/test/query-client";
import { InfiniteQueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { containersQueryOptions } from "./containers";

function container(id: string, app = "shop", fn = "checkout"): Schemas["Container"] {
  return {
    id,
    app,
    function: fn,
    release_id: "release-1",
    state: "ready",
    slots: 1,
    running_tasks: 0,
    cpu_millis: 125,
    memory_mib: 128,
    created_at: "2026-07-10T12:00:00Z",
  };
}

function api(handle: (url: URL) => unknown) {
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const url = new URL((input as Request).url);
    return Response.json(handle(url));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("container pagination", () => {
  it("asks the server for one workload's live containers and keeps what it answers", async () => {
    const fetchMock = api(() => ({ containers: [container("one"), container("two")] }));
    const page = await testQueryClient().fetchInfiniteQuery(
      containersQueryOptions("workspace", { app: "shop", function: "checkout", live: true }),
    );

    const request = new URL((fetchMock.mock.calls[0]?.[0] as Request).url);
    expect(request.pathname).toBe("/v1/workspaces/workspace/containers");
    expect(Object.fromEntries(request.searchParams)).toEqual({
      app: "shop",
      function: "checkout",
      live: "true",
      limit: "100",
    });
    expect(page.pages[0]?.data.map((item) => item.id)).toEqual(["one", "two"]);
  });

  it("asks the server for one deployment's containers, stopped ones included", async () => {
    const fetchMock = api(() => ({ containers: [container("one")] }));
    await testQueryClient().fetchInfiniteQuery(
      containersQueryOptions("workspace", { deployment: "deployment-1" }),
    );

    const request = new URL((fetchMock.mock.calls[0]?.[0] as Request).url);
    expect(Object.fromEntries(request.searchParams)).toEqual({
      deployment: "deployment-1",
      live: "false",
      limit: "100",
    });
  });

  it("bounds retained pages and refresh requests after scrolling through a long list", async () => {
    const fetchMock = api((url) => {
      const page = Number(url.searchParams.get("cursor") ?? 0);
      return { containers: [container(`container-${page}`)], next_cursor: String(page + 1) };
    });
    const client = testQueryClient({ defaultOptions: { queries: { retry: false } } });
    const observer = new InfiniteQueryObserver(client, containersQueryOptions("workspace"));
    try {
      for (let page = 0; page < 12; page++) await observer.fetchNextPage();
      const beforeRefresh = fetchMock.mock.calls.length;
      const refreshed = await observer.refetch();

      expect(refreshed.data?.pages.length).toBeLessThanOrEqual(5);
      expect(refreshed.data?.pages.at(-1)?.data[0]?.id).toBe("container-11");
      expect(fetchMock.mock.calls.length - beforeRefresh).toBeLessThanOrEqual(5);
    } finally {
      observer.destroy();
    }
  });
});

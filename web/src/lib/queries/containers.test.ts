import { testQueryClient } from "@/test/query-client";
import { InfiniteQueryObserver } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { rememberWorkspaces } from "@/lib/api/workspaces";

import { containersQueryOptions } from "./containers";

beforeEach(() => rememberWorkspaces([{ id: "workspace-1", name: "workspace" }]));

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
  it("reads live containers for live statuses and narrows them to the app's workload", async () => {
    const fetchMock = api((url) =>
      url.pathname.endsWith("/apps")
        ? { apps: [{ id: "app-1", name: "shop", state: "active", workloads: 1, created_at: "" }] }
        : {
            containers: [
              container("one"),
              container("other-workload", "shop", "refund"),
              container("other-app", "blog"),
            ],
          },
    );
    const options = containersQueryOptions("workspace-1", {
      appId: "app-1",
      stubIds: ["shop:checkout:release-1"],
      statuses: ["pending", "running"],
    });
    if (typeof options.queryFn !== "function") throw new Error("container query is missing");

    const page = await options.queryFn({
      client: testQueryClient(),
      direction: "forward",
      meta: undefined,
      pageParam: "cursor-1",
      queryKey: options.queryKey,
      signal: new AbortController().signal,
    });

    const request = new URL((fetchMock.mock.calls[0]?.[0] as Request).url);
    expect(request.pathname).toBe("/v1/workspaces/workspace/containers");
    expect(request.searchParams.get("live")).toBe("true");
    expect(request.searchParams.get("cursor")).toBe("cursor-1");
    expect(page.data.map((item) => item.container.id)).toEqual(["one"]);
    expect(page.data[0]?.app_id).toBe("app-1");
  });

  it("bounds retained pages and refresh requests after scrolling through a long list", async () => {
    const fetchMock = api((url) => {
      if (url.pathname.endsWith("/apps")) return { apps: [] };
      const page = Number(url.searchParams.get("cursor") ?? 0);
      return { containers: [container(`container-${page}`)], next_cursor: String(page + 1) };
    });
    const client = testQueryClient({ defaultOptions: { queries: { retry: false } } });
    const observer = new InfiniteQueryObserver(client, containersQueryOptions("workspace-1"));
    try {
      for (let page = 0; page < 12; page++) await observer.fetchNextPage();
      const beforeRefresh = fetchMock.mock.calls.length;
      const refreshed = await observer.refetch();

      expect(refreshed.data?.pages.length).toBeLessThanOrEqual(5);
      expect(refreshed.data?.pages.at(-1)?.data[0]?.container.id).toBe("container-11");
      expect(fetchMock.mock.calls.length - beforeRefresh).toBeLessThanOrEqual(5);
    } finally {
      observer.destroy();
    }
  });
});

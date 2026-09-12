import { testQueryClient } from "@/test/query-client";
import { InfiniteQueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { ContainerWithAppPage } from "@/lib/api/schemas";

import { containersQueryOptions } from "./containers";

describe("container pagination", () => {
  it("sends workspace, app, repeated statuses, and cursor to the API", async () => {
    const fetchMock = vi.fn<typeof fetch>();
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ data: [], next: "" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const options = containersQueryOptions("workspace-1", {
      appId: "app-1",
      stubIds: ["stub-1"],
      statuses: ["pending", "running"],
    });
    if (typeof options.queryFn !== "function") throw new Error("container query is missing");

    await options.queryFn({
      client: testQueryClient(),
      direction: "forward",
      meta: undefined,
      pageParam: "cursor-1",
      queryKey: options.queryKey,
      signal: new AbortController().signal,
    });

    const requestUrl = new URL(String(fetchMock.mock.calls[0]?.[0]), "http://localhost");
    expect(requestUrl.searchParams.getAll("status")).toEqual(["pending", "running"]);
    expect(requestUrl.searchParams.getAll("stub_id")).toEqual(["stub-1"]);
    expect(requestUrl.searchParams.get("cursor")).toBe("cursor-1");
    expect(requestUrl.searchParams.get("workspace")).toBe("workspace-1");
    expect(requestUrl.searchParams.get("app_id")).toBe("app-1");
  });

  it("bounds retained pages and refresh requests after scrolling through a long list", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const cursor = new URL(String(input), "http://localhost").searchParams.get("cursor");
      const page = Number(cursor ?? 0);
      return Response.json(containerPage([`container-${page}`], String(page + 1)));
    });
    vi.stubGlobal("fetch", fetchMock);
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

function containerPage(ids: string[], next: string): ContainerWithAppPage {
  return {
    data: ids.map((id) => ({
      app_id: "",
      container: {
        id,
        name: id,
        image: "python:3.12",
        workspace_id: "workspace-1",
        runtime_machine_id: "",
        runtime_worker_id: "",
        status: "stopped",
        termination_reason: "UNKNOWN",
        command: [],
        ports: {},
        created_at: "2026-07-10T12:00:00Z",
      },
    })),
    next,
  };
}

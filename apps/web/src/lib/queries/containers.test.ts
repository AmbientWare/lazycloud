import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { LIVE_LIST_MAX_PAGES } from "./infinite-list";

import type { ContainerWithAppPage } from "@/lib/api/schemas";

import { containersQueryOptions, nextContainerCursor, selectContainerList } from "./containers";

describe("container pagination", () => {
  it("deduplicates overlapping live pages while preserving server order", () => {
    const first = containerPage(["container-3", "container-2"], "cursor-2");
    const second = containerPage(["container-2", "container-1"], "");

    expect(
      selectContainerList({ pages: [first, second] }, false).items.map((item) => item.container.id),
    ).toEqual(["container-3", "container-2", "container-1"]);
  });

  it("stops when a server repeats a cursor", () => {
    const first = containerPage(["container-3"], "cursor-2");
    const repeated = containerPage(["container-2"], "cursor-2");

    expect(nextContainerCursor(first, [first])).toBe("cursor-2");
    expect(nextContainerCursor(repeated, [first, repeated])).toBeUndefined();
  });

  it("sends repeated typed statuses and relies on workspace live invalidation", async () => {
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
      client: new QueryClient(),
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
    expect(options.refetchInterval).toBeUndefined();
    expect(options.meta).toEqual({
      workspaceLiveEnabled: true,
      workspaceLiveCritical: true,
      workspaceLiveRecoverErrors: true,
    });
  });

  it("keeps a live list bounded, so one change event is not twenty requests", () => {
    // The change stream refetches every page a list holds. Unbounded, an app
    // view that had scrolled through two thousand containers re-requested all
    // of them each time anything in the workspace moved.
    expect(containersQueryOptions("workspace-1").maxPages).toBe(LIVE_LIST_MAX_PAGES);
    expect(LIVE_LIST_MAX_PAGES).toBeLessThanOrEqual(5);
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

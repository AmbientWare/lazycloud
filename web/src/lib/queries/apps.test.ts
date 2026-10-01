import { testQueryClient } from "@/test/query-client";
import { QueryObserver } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { rememberWorkspaces } from "@/lib/api/workspaces";

import { appSummariesQueryOptions } from "./apps";
import { appById } from "./directory";

beforeEach(() => rememberWorkspaces([{ id: "workspace-1", name: "workspace" }]));

function app(id: string, name: string): Schemas["App"] {
  return { id, name, state: "active", workloads: 0, created_at: "2026-07-10T12:00:00Z" };
}

it("lists an app deployed after the first read once the summaries refetch", async () => {
  let apps = [app("app-1", "shop")];
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const path = new URL((input as Request).url).pathname;
    if (path.endsWith("/apps")) return Response.json({ apps });
    if (path.endsWith("/deployments")) return Response.json({ deployments: [] });
    if (path.endsWith("/containers")) return Response.json({ containers: [] });
    return Response.json({ series: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = testQueryClient({ defaultOptions: { queries: { retry: false } } });
  const observer = new QueryObserver(client, appSummariesQueryOptions("workspace-1"));
  const unsubscribe = observer.subscribe(() => {});
  try {
    await vi.waitFor(() => expect(observer.getCurrentResult().data?.items).toHaveLength(1));

    apps = [...apps, app("app-2", "blog")];
    const refetched = await observer.refetch();

    expect(refetched.data?.items.map((item) => item.app.name)).toEqual(["blog", "shop"]);
    // Lookups read the directory the refetch left behind, without another request.
    const requests = fetchMock.mock.calls.length;
    expect((await appById(client, "workspace-1", "app-2")).name).toBe("blog");
    expect(fetchMock.mock.calls.length).toBe(requests);
  } finally {
    unsubscribe();
  }
});

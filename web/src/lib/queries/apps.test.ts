import { testQueryClient } from "@/test/query-client";
import { afterEach, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { appSummariesQueryOptions } from "./apps";

afterEach(() => vi.unstubAllGlobals());

function app(name: string, running = 0): Schemas["App"] {
  return {
    id: `${name}-id`,
    name,
    state: "active",
    workloads: 1,
    running_containers: running,
    created_at: "2026-07-10T12:00:00Z",
  };
}

function workload(appName: string, name: string, deployedAt: string): Schemas["DeployedWorkload"] {
  return {
    id: `${appName}-${name}`,
    app: appName,
    name,
    kind: "function",
    state: "active",
    version: 1,
    running_containers: 0,
    created_at: deployedAt,
    deployed_at: deployedAt,
  };
}

it("builds every app's card from complete lists and never reads containers", async () => {
  const requests: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async (input) => {
      const url = new URL((input as Request).url);
      requests.push(url);
      const cursor = url.searchParams.get("cursor");
      if (url.pathname.endsWith("/apps")) {
        // Two pages: an app past the first page still gets a card.
        return Response.json(
          cursor ? { apps: [app("shop", 2)] } : { apps: [app("blog", 3)], next_cursor: "blog" },
        );
      }
      if (url.pathname.endsWith("/deployments")) {
        return Response.json(
          cursor
            ? { deployments: [workload("shop", "checkout", "2026-07-10T12:00:00Z")] }
            : {
                deployments: [
                  workload("blog", "render", "2026-07-09T12:00:00Z"),
                  workload("blog", "publish", "2026-07-10T09:00:00Z"),
                ],
                next_cursor: "blog/render",
              },
        );
      }
      return Response.json({
        window_seconds: 3600,
        start: "2026-07-09T13:00:00Z",
        end: "2026-07-10T13:00:00Z",
        series: [{ app: "shop", total: 4, buckets: [] }],
      });
    }),
  );

  const summaries = await testQueryClient().fetchQuery(appSummariesQueryOptions("dev"));

  expect(summaries.map((item) => item.app.name)).toEqual(["blog", "shop"]);
  expect(summaries.map((item) => item.app.running_containers)).toEqual([3, 2]);
  expect(summaries[0]?.workloads.map((item) => item.name)).toEqual(["publish", "render"]);
  expect(summaries[1]?.workloads.map((item) => item.name)).toEqual(["checkout"]);
  expect(summaries[1]?.activity?.total).toBe(4);
  expect(requests.some((url) => url.pathname.includes("/containers"))).toBe(false);
});

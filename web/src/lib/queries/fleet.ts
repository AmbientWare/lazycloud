import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";
import { nextListCursor } from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

export function fleetSummaryQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.admin.fleet.summary(),
    queryFn: () => ok(api.GET("/v1/fleet")),
  });
}

export function fleetNodesQueryOptions() {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.admin.fleet.nodes(),
    initialPageParam: "",
    queryFn: async ({ pageParam }) => {
      const page = await ok(
        api.GET("/v1/fleet/nodes", {
          params: { query: { limit: 50, cursor: pageParam || undefined } },
        }),
      );
      return { data: page.nodes, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
  });
}

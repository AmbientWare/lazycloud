import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import { fleetNodeListSchema, fleetSummarySchema } from "@/lib/api/schemas";
import { nextListCursor } from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

export function fleetSummaryQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.admin.fleet.summary(),
    queryFn: () => apiRequest("/api/v1/fleet", fleetSummarySchema),
  });
}

export function fleetNodesQueryOptions() {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.admin.fleet.nodes(),
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "50" });
      if (pageParam) params.set("cursor", pageParam);
      return apiRequest(`/api/v1/fleet/nodes?${params}`, fleetNodeListSchema);
    },
    getNextPageParam: nextListCursor,
  });
}

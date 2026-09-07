import { infiniteQueryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { resourceSearchResponseSchema } from "@/lib/api/schemas/search";
import { workspaceQueryKeys } from "./workspace-keys";

export function resourceSearchQueryOptions(workspaceId: string, search: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.search(workspaceId, search),
    enabled: search.trim().length > 0,
    initialPageParam: "",
    queryFn: ({ pageParam, signal }) =>
      apiRequest(
        withWorkspace(
          `/api/v1/search?${new URLSearchParams({ q: search, cursor: pageParam })}`,
          workspaceId,
        ),
        resourceSearchResponseSchema,
        { signal },
      ),
    getNextPageParam: (page) => page.next || undefined,
  });
}

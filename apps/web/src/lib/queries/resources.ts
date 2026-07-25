import { queryOptions } from "@tanstack/react-query";

import type { ResourceConfig } from "@/lib/api/resources";

import { workspaceQueryKeys } from "./workspace-keys";

export function resourceQueryOptions(config: ResourceConfig, workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.resources.list(workspaceId, config.key),
    queryFn: () => config.fetchRows(workspaceId),
    refetchInterval: 30_000,
  });
}

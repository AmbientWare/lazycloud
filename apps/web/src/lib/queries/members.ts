import { queryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import { workspaceMemberListSchema } from "@/lib/api/schemas";

import { workspaceQueryKeys } from "./workspace-keys";

export function workspaceMembersQueryOptions(workspaceId: string, workspaceName: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.members(workspaceId),
    queryFn: () =>
      apiRequest(
        `/api/v1/workspaces/${encodeURIComponent(workspaceName)}/members`,
        workspaceMemberListSchema,
      ),
    staleTime: 30_000,
  });
}

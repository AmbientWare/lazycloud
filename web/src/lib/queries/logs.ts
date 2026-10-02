import { queryOptions } from "@tanstack/react-query";

import { readLogHistory, type LogSource } from "@/lib/api/logs";

import { workspaceQueryKeys } from "./workspace-keys";

/** The newest stored lines of a source. History reads forward only, so there is no older page. */
export function logHistoryQueryOptions(workspace: string, source: LogSource) {
  return queryOptions({
    queryKey: workspaceQueryKeys.logs.history(workspace, source),
    queryFn: ({ signal }) => readLogHistory(workspace, source, signal),
  });
}

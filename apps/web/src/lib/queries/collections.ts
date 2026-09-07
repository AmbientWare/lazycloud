import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { encodedValueSchema, mapKeysSchema, queueSizeSchema } from "@/lib/api/schemas";

import { workspaceQueryKeys } from "./workspace-keys";

/**
 * Queues and maps are written by running user code, which the change stream
 * says nothing about: its topics cover the platform's own records. A queue
 * draining is exactly what somebody has this inspector open to watch, so it
 * asks, and stops asking with the tab.
 */
const LIVE_INTERVAL_MS = 2_000;

export function queueSizeQueryOptions(workspaceId: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.queueSize(workspaceId, name),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/simplequeues/${encodePath(name)}/size`, workspaceId),
        queueSizeSchema,
      ),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function queuePeekQueryOptions(workspaceId: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.queuePeek(workspaceId, name),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/simplequeues/${encodePath(name)}/peek`, workspaceId),
        encodedValueSchema,
      ),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function mapKeysQueryOptions(workspaceId: string, name: string, search: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.collections.mapKeys(workspaceId, name, search),
    initialPageParam: "",
    queryFn: ({ pageParam, signal }) =>
      apiRequest(
        withWorkspace(
          `/api/v1/maps/${encodePath(name)}/keys?${new URLSearchParams({ cursor: pageParam, q: search })}`,
          workspaceId,
        ),
        mapKeysSchema,
        { signal },
      ),
    getNextPageParam: (page) => page.next || undefined,
  });
}

export function mapValueQueryOptions(workspaceId: string, name: string, key: string) {
  const params = new URLSearchParams({ key });
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.mapValue(workspaceId, name, key),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/maps/${encodePath(name)}/get?${params.toString()}`, workspaceId),
        encodedValueSchema,
      ),
    enabled: Boolean(key),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

/** Collection names may contain slashes; keep them path-safe segment by segment. */
function encodePath(name: string): string {
  return name.split("/").map(encodeURIComponent).join("/");
}

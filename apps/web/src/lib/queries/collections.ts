import { queryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import {
  encodedValueSchema,
  mapCountSchema,
  mapKeysSchema,
  queueSizeSchema,
} from "@/lib/api/schemas";

import { workspaceQueryKeys } from "./workspace-keys";

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

export function mapCountQueryOptions(workspaceId: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.mapCount(workspaceId, name),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/maps/${encodePath(name)}/count`, workspaceId),
        mapCountSchema,
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

export function mapKeysQueryOptions(workspaceId: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.mapKeys(workspaceId, name),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/maps/${encodePath(name)}/keys`, workspaceId),
        mapKeysSchema,
      ),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function mapValueQueryOptions(
  workspaceId: string,
  name: string,
  key: string,
) {
  const params = new URLSearchParams({ key });
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.mapValue(workspaceId, name, key),
    queryFn: () =>
      apiRequest(
        withWorkspace(
          `/api/v1/maps/${encodePath(name)}/get?${params.toString()}`,
          workspaceId,
        ),
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

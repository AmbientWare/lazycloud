import {
  infiniteQueryOptions,
  queryOptions,
  skipToken,
  type QueryClient,
} from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  encodedValueSchema,
  mapCountSchema,
  mapKeyPageSchema,
  mapEntrySchema,
  collectionWriteSchema,
  jsonValueSchema,
  queueSizeSchema,
} from "@/lib/api/schemas";

import { workspaceQueryKeys } from "./workspace-keys";
import { fileBase64 } from "@/lib/files";

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

export function mapKeysQueryOptions(workspaceId: string, name: string, prefix = "") {
  return infiniteQueryOptions({
    queryKey: [...workspaceQueryKeys.collections.mapKeys(workspaceId, name), prefix],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      apiRequest(
        withWorkspace(
          `/api/v1/maps/${encodePath(name)}/entries?${new URLSearchParams({
            prefix,
            ...(pageParam ? { cursor: pageParam } : {}),
          })}`,
          workspaceId,
        ),
        mapKeyPageSchema,
      ),
    getNextPageParam: (page) => page.next ?? undefined,
    refetchInterval: 15_000,
  });
}

export function mapValueQueryOptions(workspaceId: string, name: string, key: string | null) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.mapValue(workspaceId, name, key),
    queryFn:
      key === null
        ? skipToken
        : () =>
            apiRequest(
              withWorkspace(
                `/api/v1/maps/${encodePath(name)}/entry?${new URLSearchParams({ key })}`,
                workspaceId,
              ),
              mapEntrySchema,
            ),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function parseCollectionJson(text: string): string {
  const parsed = jsonValueSchema.safeParse(JSON.parse(text));
  if (!parsed.success) throw new Error("Enter a valid JSON value.");
  const serialized = JSON.stringify(parsed.data);
  if (new TextEncoder().encode(serialized).length > 1024 * 1024) {
    throw new Error("Keep the JSON value under 1 MiB.");
  }
  return serialized;
}

export async function setMapValue(
  workspaceId: string,
  name: string,
  key: string,
  json: string,
  ttlSeconds: number | null,
  revision?: string,
) {
  return postJson(
    withWorkspace(`/api/v1/maps/${encodePath(name)}/set`, workspaceId),
    collectionWriteSchema,
    {
      key,
      value_base64: await fileBase64(new Blob([parseCollectionJson(json)])),
      ttl_seconds: ttlSeconds,
      ...(revision === undefined ? { if_absent: true } : { if_revision: revision }),
    },
  );
}

export function deleteMapKey(workspaceId: string, name: string, key: string, revision: string) {
  return postJson(
    withWorkspace(`/api/v1/maps/${encodePath(name)}/delete`, workspaceId),
    collectionWriteSchema,
    { key, if_revision: revision },
  );
}

export async function putQueueMessage(workspaceId: string, name: string, json: string) {
  return postJson(
    withWorkspace(`/api/v1/simplequeues/${encodePath(name)}/put`, workspaceId),
    collectionWriteSchema,
    { value_base64: await fileBase64(new Blob([parseCollectionJson(json)])) },
  );
}

export function popQueueMessage(workspaceId: string, name: string) {
  return postJson(
    withWorkspace(`/api/v1/simplequeues/${encodePath(name)}/pop`, workspaceId),
    encodedValueSchema,
  );
}

export function deleteCollection(workspaceId: string, kind: "maps" | "queues", name: string) {
  return apiRequest(
    withWorkspace(
      `/api/v1/${kind === "maps" ? "maps" : "simplequeues"}/${encodePath(name)}`,
      workspaceId,
    ),
    z.null(),
    { method: "DELETE" },
  );
}

export async function refreshCollection(
  client: QueryClient,
  workspaceId: string,
  kind: "maps" | "queues",
  name: string,
) {
  await Promise.all([
    client.invalidateQueries({ queryKey: workspaceQueryKeys.resources.list(workspaceId, kind) }),
    client.invalidateQueries({
      queryKey: workspaceQueryKeys.collections.resource(
        workspaceId,
        kind === "maps" ? "map" : "queue",
        name,
      ),
    }),
  ]);
}

/** Collection names may contain slashes; keep them path-safe segment by segment. */
function encodePath(name: string): string {
  return name.split("/").map(encodeURIComponent).join("/");
}

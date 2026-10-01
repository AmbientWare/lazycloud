import {
  infiniteQueryOptions,
  queryOptions,
  skipToken,
  type QueryClient,
} from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { jsonValueSchema } from "@/lib/api/schemas";
import { fileBase64 } from "@/lib/files";
import { nextPageCursor, selectPages } from "@/lib/queries/infinite-list";

import { workspaceQueryKeys } from "./workspace-keys";

export type CollectionKind = "queues" | "maps";
export type QueueInfo = Schemas["QueueInfo"];
export type MapInfo = Schemas["MapInfo"];
export type MapEntry = Schemas["MapEntry"];

/**
 * Queues and maps are written by running user code, which the change stream
 * says nothing about: its topics cover the platform's own records. A queue
 * draining is exactly what somebody has this inspector open to watch, so it
 * asks, and stops asking with the tab.
 */
const LIVE_INTERVAL_MS = 2_000;
const LIST_INTERVAL_MS = 30_000;

export function queuesQueryOptions(workspace: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.collections.list(workspace, "queues"),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/queues", {
          params: { path: { workspace }, query: { limit: 100, cursor: pageParam || undefined } },
        }),
      ),
    getNextPageParam: nextPageCursor,
    refetchInterval: LIST_INTERVAL_MS,
  });
}

export function selectQueues(
  data: { pages: readonly Schemas["QueuePage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.queues,
    hasNextPage,
    (queue) => queue.name,
  );
}

export function mapsQueryOptions(workspace: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.collections.list(workspace, "maps"),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/maps", {
          params: { path: { workspace }, query: { limit: 100, cursor: pageParam || undefined } },
        }),
      ),
    getNextPageParam: nextPageCursor,
    refetchInterval: LIST_INTERVAL_MS,
  });
}

export function selectMaps(
  data: { pages: readonly Schemas["MapPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.maps,
    hasNextPage,
    (map) => map.name,
  );
}

/** The exact size and oldest message; a queue never written has size 0. */
export function queueQueryOptions(workspace: string, queue: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.item(workspace, "queues", queue),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/queues/{queue}", {
          params: { path: { workspace, queue } },
        }),
      ),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

/** The oldest message without removing it; `message` is absent when the queue is empty. */
export function queueHeadQueryOptions(workspace: string, queue: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.queueHead(workspace, queue),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/queues/{queue}/head", {
          params: { path: { workspace, queue } },
        }),
      ),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

/** Exact key count and expiry statistics; a map never written is empty. */
export function mapQueryOptions(workspace: string, map: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.item(workspace, "maps", map),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/maps/{map}", {
          params: { path: { workspace, map } },
        }),
      ),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function mapKeysQueryOptions(workspace: string, map: string, prefix: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.collections.mapKeys(workspace, map, prefix),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/maps/{map}/keys", {
          params: {
            path: { workspace, map },
            query: { prefix: prefix || undefined, limit: 200, cursor: pageParam || undefined },
          },
        }),
      ),
    getNextPageParam: nextPageCursor,
    refetchInterval: 15_000,
  });
}

export function selectMapKeys(
  data: { pages: readonly Schemas["MapKeyPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.keys,
    hasNextPage,
    (key) => key,
  );
}

export function mapEntryQueryOptions(workspace: string, map: string, key: string | null) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.mapEntry(workspace, map, key),
    queryFn:
      key === null
        ? skipToken
        : () =>
            ok(
              api.GET("/v1/workspaces/{workspace}/maps/{map}/entries/{key}", {
                params: { path: { workspace, map, key } },
              }),
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

function encodeJson(json: string): Promise<string> {
  return fileBase64(new Blob([parseCollectionJson(json)]));
}

/**
 * Write a key. With `revision` the write applies only while the key still
 * holds it; without one only while the key is missing. Either failed
 * condition is a conflict. A null `ttlSeconds` keeps the current expiry, 0
 * removes it.
 */
export async function setMapValue(
  workspace: string,
  map: string,
  key: string,
  json: string,
  ttlSeconds: number | null,
  revision?: string,
): Promise<Schemas["MapEntryWrite"]> {
  return ok(
    api.PUT("/v1/workspaces/{workspace}/maps/{map}/entries/{key}", {
      params: { path: { workspace, map, key } },
      body: {
        value: await encodeJson(json),
        ...(ttlSeconds === null ? {} : { ttl_seconds: ttlSeconds }),
        ...(revision === undefined
          ? { if_absent: true }
          : { if_absent: false, if_revision: revision }),
      },
    }),
  );
}

/** Deletes the key only while it still holds `revision`. */
export function deleteMapKey(
  workspace: string,
  map: string,
  key: string,
  revision: string,
): Promise<void> {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/maps/{map}/entries/{key}", {
      params: { path: { workspace, map, key }, query: { if_revision: revision } },
    }),
  );
}

export async function putQueueMessage(workspace: string, queue: string, json: string) {
  return ok(
    api.POST("/v1/workspaces/{workspace}/queues/{queue}/messages", {
      params: { path: { workspace, queue } },
      body: { messages: [await encodeJson(json)] },
    }),
  );
}

/** Removes and returns the oldest message; `message` is absent when the queue was empty. */
export function popQueueMessage(
  workspace: string,
  queue: string,
): Promise<Schemas["QueueMessageResult"]> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/queues/{queue}/pop", {
      params: { path: { workspace, queue } },
    }),
  );
}

export function deleteCollection(
  workspace: string,
  kind: CollectionKind,
  name: string,
): Promise<void> {
  return kind === "maps"
    ? ok(
        api.DELETE("/v1/workspaces/{workspace}/maps/{map}", {
          params: { path: { workspace, map: name } },
        }),
      )
    : ok(
        api.DELETE("/v1/workspaces/{workspace}/queues/{queue}", {
          params: { path: { workspace, queue: name } },
        }),
      );
}

/** Refetch the kind's list and every open inspector of it, which share its key prefix. */
export function refreshCollection(client: QueryClient, workspace: string, kind: CollectionKind) {
  return client.invalidateQueries({
    queryKey: workspaceQueryKeys.collections.list(workspace, kind),
  });
}

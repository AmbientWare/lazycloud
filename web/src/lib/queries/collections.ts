import {
  infiniteQueryOptions,
  queryOptions,
  skipToken,
  type QueryClient,
} from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";
import { fileBase64 } from "@/lib/files";

import { allPages } from "./storage";
import { workspaceQueryKeys } from "./workspace-keys";

export type CollectionKind = "maps" | "queues";

/**
 * Queues and maps are written by running user code, which the change stream
 * says nothing about: its topics cover the platform's own records. A queue
 * draining is exactly what somebody has this inspector open to watch, so it
 * asks, and stops asking with the tab.
 */
const LIVE_INTERVAL_MS = 2_000;

const queuePath = (workspace: string, queue: string) => ({
  params: { path: { workspace, queue } },
});

const mapPath = (workspace: string, map: string) => ({
  params: { path: { workspace, map } },
});

const entryPath = (workspace: string, map: string, key: string) => ({
  params: { path: { workspace, map, key } },
});

export function queuesQueryOptions(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.list(workspace, "queues"),
    queryFn: () =>
      allPages(async (cursor) => {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/queues", {
            params: { path: { workspace }, query: { limit: 100, cursor } },
          }),
        );
        return { items: page.queues, next: page.next_cursor };
      }),
    refetchInterval: 30_000,
  });
}

export function mapsQueryOptions(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.list(workspace, "maps"),
    queryFn: () =>
      allPages(async (cursor) => {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/maps", {
            params: { path: { workspace }, query: { limit: 100, cursor } },
          }),
        );
        return { items: page.maps, next: page.next_cursor };
      }),
    refetchInterval: 30_000,
  });
}

export function queueQueryOptions(workspace: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.queue(workspace, name),
    queryFn: () =>
      ok(api.GET("/v1/workspaces/{workspace}/queues/{queue}", queuePath(workspace, name))),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function mapQueryOptions(workspace: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.map(workspace, name),
    queryFn: () => ok(api.GET("/v1/workspaces/{workspace}/maps/{map}", mapPath(workspace, name))),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function queueHeadQueryOptions(workspace: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.queueHead(workspace, name),
    queryFn: () =>
      ok(api.GET("/v1/workspaces/{workspace}/queues/{queue}/head", queuePath(workspace, name))),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function mapKeysQueryOptions(workspace: string, name: string, prefix = "") {
  return infiniteQueryOptions({
    queryKey: [...workspaceQueryKeys.collections.mapKeys(workspace, name), prefix],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/maps/{map}/keys", {
          params: {
            ...mapPath(workspace, name).params,
            query: { prefix: prefix || undefined, cursor: pageParam || undefined },
          },
        }),
      ),
    getNextPageParam: (page) => page.next_cursor,
    refetchInterval: 15_000,
  });
}

export function mapValueQueryOptions(workspace: string, name: string, key: string | null) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.mapValue(workspace, name, key),
    queryFn:
      key === null
        ? skipToken
        : () =>
            ok(
              api.GET(
                "/v1/workspaces/{workspace}/maps/{map}/entries/{key}",
                entryPath(workspace, name, key),
              ),
            ),
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

/** The compact JSON a value is stored as; a parse failure throws. */
export function parseCollectionJson(text: string): string {
  const serialized = JSON.stringify(JSON.parse(text));
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
 * holds it, without one only while the key is missing; either failed
 * condition is a conflict. A null `ttlSeconds` keeps the current expiry.
 */
export async function setMapValue(
  workspace: string,
  name: string,
  key: string,
  json: string,
  ttlSeconds: number | null,
  revision?: string,
) {
  return ok(
    api.PUT("/v1/workspaces/{workspace}/maps/{map}/entries/{key}", {
      ...entryPath(workspace, name, key),
      body: {
        value: await encodeJson(json),
        ...(ttlSeconds === null ? {} : { ttl_seconds: ttlSeconds }),
        ...(revision === undefined ? { if_absent: true } : { if_revision: revision }),
      },
    }),
  );
}

export function deleteMapKey(workspace: string, name: string, key: string, revision: string) {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/maps/{map}/entries/{key}", {
      params: { ...entryPath(workspace, name, key).params, query: { if_revision: revision } },
    }),
  );
}

export async function putQueueMessage(workspace: string, name: string, json: string) {
  return ok(
    api.POST("/v1/workspaces/{workspace}/queues/{queue}/messages", {
      ...queuePath(workspace, name),
      body: { messages: [await encodeJson(json)] },
    }),
  );
}

export function popQueueMessage(workspace: string, name: string) {
  return ok(api.POST("/v1/workspaces/{workspace}/queues/{queue}/pop", queuePath(workspace, name)));
}

export async function deleteCollection(workspace: string, kind: CollectionKind, name: string) {
  if (kind === "maps") {
    await ok(api.DELETE("/v1/workspaces/{workspace}/maps/{map}", mapPath(workspace, name)));
  } else {
    await ok(api.DELETE("/v1/workspaces/{workspace}/queues/{queue}", queuePath(workspace, name)));
  }
}

export async function refreshCollection(
  client: QueryClient,
  workspace: string,
  kind: CollectionKind,
  name: string,
) {
  await Promise.all([
    client.invalidateQueries({ queryKey: workspaceQueryKeys.collections.list(workspace, kind) }),
    client.invalidateQueries({
      queryKey: workspaceQueryKeys.collections.resource(
        workspace,
        kind === "maps" ? "map" : "queue",
        name,
      ),
    }),
  ]);
}

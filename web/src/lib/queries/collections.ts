import {
  infiniteQueryOptions,
  queryOptions,
  skipToken,
  type QueryClient,
} from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";
import { jsonValueSchema, type MapEntry } from "@/lib/api/schemas";
import { workspaceName } from "@/lib/api/workspaces";
import { fileBase64 } from "@/lib/files";

import { workspaceQueryKeys } from "./workspace-keys";

/**
 * Queues and maps are written by running user code, which the change stream
 * says nothing about: its topics cover the platform's own records. A queue
 * draining is exactly what somebody has this inspector open to watch, so it
 * asks, and stops asking with the tab.
 */
const LIVE_INTERVAL_MS = 2_000;

const queuePath = (workspaceId: string, queue: string) => ({
  params: { path: { workspace: workspaceName(workspaceId), queue } },
});

const mapPath = (workspaceId: string, map: string) => ({
  params: { path: { workspace: workspaceName(workspaceId), map } },
});

const entryPath = (workspaceId: string, map: string, key: string) => ({
  params: { path: { workspace: workspaceName(workspaceId), map, key } },
});

export function queueSizeQueryOptions(workspaceId: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.queueSize(workspaceId, name),
    queryFn: async (): Promise<{ size: number }> => {
      const queue = await ok(
        api.GET("/v1/workspaces/{workspace}/queues/{queue}", queuePath(workspaceId, name)),
      );
      return { size: queue.size };
    },
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function mapCountQueryOptions(workspaceId: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.mapCount(workspaceId, name),
    queryFn: async (): Promise<{ count: number }> => {
      const map = await ok(
        api.GET("/v1/workspaces/{workspace}/maps/{map}", mapPath(workspaceId, name)),
      );
      return { count: map.count };
    },
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function queuePeekQueryOptions(workspaceId: string, name: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.collections.queuePeek(workspaceId, name),
    queryFn: async (): Promise<{ value_base64: string }> => {
      const head = await ok(
        api.GET("/v1/workspaces/{workspace}/queues/{queue}/head", queuePath(workspaceId, name)),
      );
      return { value_base64: head.message ?? "" };
    },
    refetchInterval: LIVE_INTERVAL_MS,
  });
}

export function mapKeysQueryOptions(workspaceId: string, name: string, prefix = "") {
  return infiniteQueryOptions({
    queryKey: [...workspaceQueryKeys.collections.mapKeys(workspaceId, name), prefix],
    initialPageParam: "",
    queryFn: async ({ pageParam }): Promise<{ data: string[]; next: string | null }> => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/maps/{map}/keys", {
          params: {
            ...mapPath(workspaceId, name).params,
            query: { prefix: prefix || undefined, cursor: pageParam || undefined },
          },
        }),
      );
      return { data: page.keys, next: page.next_cursor ?? null };
    },
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
        : async (): Promise<MapEntry> => {
            const entry = await ok(
              api.GET(
                "/v1/workspaces/{workspace}/maps/{map}/entries/{key}",
                entryPath(workspaceId, name, key),
              ),
            );
            return {
              value_base64: entry.value,
              revision: entry.revision,
              expires_at: entry.expires_at ?? null,
            };
          },
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
 * holds it, without one only while the key is missing; either failed
 * condition is a conflict. A null `ttlSeconds` keeps the current expiry.
 */
export async function setMapValue(
  workspaceId: string,
  name: string,
  key: string,
  json: string,
  ttlSeconds: number | null,
  revision?: string,
) {
  await ok(
    api.PUT("/v1/workspaces/{workspace}/maps/{map}/entries/{key}", {
      ...entryPath(workspaceId, name, key),
      body: {
        value: await encodeJson(json),
        ...(ttlSeconds === null ? {} : { ttl_seconds: ttlSeconds }),
        ...(revision === undefined
          ? { if_absent: true }
          : { if_absent: false, if_revision: revision }),
      },
    }),
  );
  return {};
}

export async function deleteMapKey(
  workspaceId: string,
  name: string,
  key: string,
  revision: string,
) {
  await ok(
    api.DELETE("/v1/workspaces/{workspace}/maps/{map}/entries/{key}", {
      params: { ...entryPath(workspaceId, name, key).params, query: { if_revision: revision } },
    }),
  );
  return {};
}

export async function putQueueMessage(workspaceId: string, name: string, json: string) {
  await ok(
    api.POST("/v1/workspaces/{workspace}/queues/{queue}/messages", {
      ...queuePath(workspaceId, name),
      body: { messages: [await encodeJson(json)] },
    }),
  );
  return {};
}

export async function popQueueMessage(
  workspaceId: string,
  name: string,
): Promise<{ value_base64: string }> {
  const result = await ok(
    api.POST("/v1/workspaces/{workspace}/queues/{queue}/pop", queuePath(workspaceId, name)),
  );
  return { value_base64: result.message ?? "" };
}

export async function deleteCollection(
  workspaceId: string,
  kind: "maps" | "queues",
  name: string,
): Promise<null> {
  if (kind === "maps") {
    await ok(api.DELETE("/v1/workspaces/{workspace}/maps/{map}", mapPath(workspaceId, name)));
  } else {
    await ok(api.DELETE("/v1/workspaces/{workspace}/queues/{queue}", queuePath(workspaceId, name)));
  }
  return null;
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

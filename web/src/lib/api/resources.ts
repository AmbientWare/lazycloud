import { api, ok, type Schemas } from "@/lib/api/client";
import { workspaceName } from "@/lib/api/workspaces";

export type RowValue = string | number | boolean | null | undefined;
export type ResourceRow = Record<string, RowValue> & { id: string };

export type ResourceConfig = {
  key: string;
  title: string;
  fetchRows: (workspaceId: string) => Promise<ResourceRow[]>;
};

/** Every page of a collection; the storage tabs list all of it. */
async function allPages<T>(
  read: (cursor: string | undefined) => Promise<{ items: T[]; next?: string }>,
): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | undefined;
  do {
    const page = await read(cursor);
    items.push(...page.items);
    cursor = page.next;
  } while (cursor);
  return items;
}

/** Seconds from now back to `at`, or until it; null when absent. */
function secondsFrom(at: string | undefined, now: number): number | null {
  return at ? Math.max(0, Math.abs(Date.parse(at) - now) / 1_000) : null;
}

function queueRow(queue: Schemas["QueueInfo"], now: number): ResourceRow {
  return {
    id: queue.name,
    name: queue.name,
    size: queue.size,
    oldest_message_age_seconds: secondsFrom(queue.oldest_message_at, now),
    // Queues keep no put-rate statistic.
    put_rate_per_minute: 0,
  };
}

function mapRow(map: Schemas["MapInfo"], now: number): ResourceRow {
  return {
    id: map.name,
    name: map.name,
    keys: map.count,
    size_bytes: map.size_bytes,
    expiring_keys: map.expiring_count,
    nearest_expiry_seconds: secondsFrom(map.next_expiry_at, now),
  };
}

export const collectionResources: ResourceConfig[] = [
  {
    key: "queues",
    title: "Queues",
    fetchRows: async (workspaceId) => {
      const workspace = workspaceName(workspaceId);
      const queues = await allPages(async (cursor) => {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/queues", {
            params: { path: { workspace }, query: { limit: 100, cursor } },
          }),
        );
        return { items: page.queues, next: page.next_cursor };
      });
      const now = Date.now();
      return queues.map((queue) => queueRow(queue, now));
    },
  },
  {
    key: "maps",
    title: "Maps",
    fetchRows: async (workspaceId) => {
      const workspace = workspaceName(workspaceId);
      const maps = await allPages(async (cursor) => {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/maps", {
            params: { path: { workspace }, query: { limit: 100, cursor } },
          }),
        );
        return { items: page.maps, next: page.next_cursor };
      });
      const now = Date.now();
      return maps.map((map) => mapRow(map, now));
    },
  },
];

import type { z } from "zod";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { mapListSchema, queueListSchema } from "@/lib/api/schemas";

export type RowValue = string | number | boolean | null | undefined;
export type ResourceRow = Record<string, RowValue> & { id: string };

export type ResourceConfig = {
  key: string;
  title: string;
  fetchRows: (workspaceId: string) => Promise<ResourceRow[]>;
};

function defineResource<T>(options: {
  key: string;
  title: string;
  path: string;
  schema: z.ZodType<T, z.ZodTypeDef, unknown>;
  rows: (response: T) => ResourceRow[];
}): ResourceConfig {
  const { path, schema, rows, ...config } = options;
  return {
    ...config,
    fetchRows: async (workspaceId) =>
      rows(await apiRequest(withWorkspace(path, workspaceId), schema)),
  };
}

export const collectionResources: ResourceConfig[] = [
  defineResource({
    key: "queues",
    title: "Queues",
    path: "/api/v1/simplequeues",
    schema: queueListSchema,
    rows: (response) => response.queues.map((item) => ({ ...item, id: item.name })),
  }),
  defineResource({
    key: "maps",
    title: "Maps",
    path: "/api/v1/maps",
    schema: mapListSchema,
    rows: (response) =>
      response.maps.map((item) => ({
        id: item.name,
        name: item.name,
        keys: item.count,
        size_bytes: item.size_bytes,
        expiring_keys: item.expiring_keys,
        nearest_expiry_seconds: item.nearest_expiry_seconds,
      })),
  }),
];

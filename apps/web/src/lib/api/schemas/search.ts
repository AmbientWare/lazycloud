import { z } from "zod";

const resourceSearchFields = {
  id: z.string(),
  name: z.string(),
  app_id: z.string().nullable(),
};
export const resourceSearchResultSchema = z.discriminatedUnion("kind", [
  z.object({ ...resourceSearchFields, kind: z.literal("app") }),
  z.object({ ...resourceSearchFields, kind: z.literal("workload"), app_id: z.string().min(1) }),
  z.object({ ...resourceSearchFields, kind: z.literal("task") }),
  z.object({ ...resourceSearchFields, kind: z.literal("sandbox") }),
]);
export type ResourceSearchResult = z.infer<typeof resourceSearchResultSchema>;
export const resourceSearchResponseSchema = z.object({
  data: z.array(resourceSearchResultSchema),
  next: z.string(),
});

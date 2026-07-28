import { z } from "zod";

/**
 * Files a run produced. Kept in step with `shared.http.artifacts` — the
 * `content_type` is what decides how the task panel renders one, so it is the
 * field most worth being strict about.
 */
export const artifactSummarySchema = z.object({
  id: z.string(),
  task_id: z.string(),
  filename: z.string(),
  content_type: z.string().default("application/octet-stream"),
  size: z.number().nonnegative().default(0),
  created_at: z.string().nullable().default(null),
});
export type ArtifactSummary = z.infer<typeof artifactSummarySchema>;

export const artifactListSchema = z.object({
  data: z.array(artifactSummarySchema).default([]),
  next: z.string().default(""),
});
export type ArtifactList = z.infer<typeof artifactListSchema>;

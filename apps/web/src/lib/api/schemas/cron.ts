import { z } from "zod";

// Synced to packages/shared/src/shared/http/operations.py (CronJobResponse,
// CronJobListResponse); scoped to the fields the workload detail page renders.

const cronJobSchema = z.object({
  name: z.string(),
  cron: z.string(),
  deployment_id: z.string(),
  enabled: z.boolean().default(true),
  last_run_at: z.string().nullish(),
  next_run_at: z.string().nullish(),
});
export type CronJob = z.infer<typeof cronJobSchema>;

export const cronJobListSchema = z.object({
  cron_jobs: z.array(cronJobSchema).default([]),
});

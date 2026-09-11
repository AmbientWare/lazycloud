import { z } from "zod";

export const taskPendingProgressSchema = z.object({
  reason: z.enum([
    "queued",
    "dependencies",
    "retry",
    "capacity_busy",
    "capacity_unavailable",
    "capacity_limit",
    "provisioning_compute",
    "starting_container",
  ]),
  message: z.string(),
  since: z.string().datetime({ offset: true }),
  pending_since: z.string().datetime({ offset: true }),
  observed_at: z.string().datetime({ offset: true }),
});
export type TaskPendingProgress = z.infer<typeof taskPendingProgressSchema>;

import { z } from "zod";

export const BillingCycleResponseSchema = z.object({
  current_period_start: z.string(),
  current_period_end: z.string(),
});

export type BillingCycleResponse = z.infer<typeof BillingCycleResponseSchema>;

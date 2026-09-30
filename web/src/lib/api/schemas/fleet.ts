import { z } from "zod";

export const fleetStateSchema = z.enum([
  "serving",
  "starting",
  "draining",
  "preparing",
  "stopping",
  "unavailable",
  "failed",
  "terminating",
  "stopped",
  "hibernate_unverified",
  "image_saved",
]);
export type FleetState = z.infer<typeof fleetStateSchema>;

export const fleetCapacitySchema = z.object({
  cpu_millicores: z.number().int(),
  memory_mib: z.number().int(),
  gpu_count: z.number().int(),
});
export type FleetCapacity = z.infer<typeof fleetCapacitySchema>;

export const fleetStateCapacitySchema = z.object({
  state: fleetStateSchema,
  machines: z.number().int(),
  capacity: fleetCapacitySchema,
  allocated: fleetCapacitySchema,
});
export type FleetStateCapacity = z.infer<typeof fleetStateCapacitySchema>;

export const fleetMarketSchema = z.object({
  preemptible: z.boolean(),
  gpu_type: z.string(),
  warm_free: fleetCapacitySchema,
  warm_target: fleetCapacitySchema,
  reserve_ready: fleetCapacitySchema,
  reserve_target: fleetCapacitySchema,
  allocated: fleetCapacitySchema,
  states: z.array(fleetStateCapacitySchema),
  reason: z.string(),
});
export type FleetMarket = z.infer<typeof fleetMarketSchema>;

export const fleetPlanSchema = z.object({
  generated_at: z.string().datetime({ offset: true }),
  expires_at: z.string().datetime({ offset: true }),
  markets: z.array(fleetMarketSchema),
});
export type FleetPlan = z.infer<typeof fleetPlanSchema>;

export const fleetReleasePhaseSchema = z.enum([
  "current",
  "waiting_for_capacity",
  "draining",
  "updating",
  "verifying",
  "preparing_reserve",
  "pending_reserve",
  "offline",
  "blocked",
]);
export type FleetReleasePhase = z.infer<typeof fleetReleasePhaseSchema>;

export const fleetReleaseSchema = z.object({
  version: z.string(),
  generation: z.number().int(),
  complete: z.boolean(),
  phases: z.record(fleetReleasePhaseSchema, z.number().int()),
  pending_capacity_owners: z.number().int(),
});
export type FleetRelease = z.infer<typeof fleetReleaseSchema>;

export const fleetSummarySchema = z.object({
  observed_at: z.string().datetime({ offset: true }),
  plan: fleetPlanSchema.nullable(),
  release: fleetReleaseSchema.nullable(),
});
export type FleetSummary = z.infer<typeof fleetSummarySchema>;

export const fleetNodeSchema = z.object({
  id: z.string(),
  machine_id: z.string().nullable(),
  instance_id: z.string().nullable(),
  provider: z.string(),
  region: z.string(),
  instance_type: z.string(),
  preemptible: z.boolean(),
  gpu_type: z.string(),
  state: fleetStateSchema,
  capacity: fleetCapacitySchema,
  allocated: fleetCapacitySchema,
  containers: z.number().int(),
  ready: z.boolean(),
});
export type FleetNode = z.infer<typeof fleetNodeSchema>;

export const fleetNodeListSchema = z.object({
  data: z.array(fleetNodeSchema),
  next: z.string(),
  observed_at: z.string().datetime({ offset: true }),
});
export type FleetNodeList = z.infer<typeof fleetNodeListSchema>;

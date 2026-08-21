import { z } from "zod";

/**
 * A resource a workload states as a reservation, or as a `[reserve, limit]` pair.
 *
 * The pair arrives as a JSON array, so it parses as a tuple here even though its
 * author wrote a Python tuple. Mirrors `CpuRequest` and `MemoryRequest` in
 * `shared.deployment_records`.
 */
export const cpuRequestSchema = z.union([z.number(), z.tuple([z.number(), z.number()])]);

const memoryValue = z.union([z.string(), z.number()]);

export const memoryRequestSchema = z.union([memoryValue, z.tuple([memoryValue, memoryValue])]);

export type CpuRequest = z.infer<typeof cpuRequestSchema>;
export type MemoryRequest = z.infer<typeof memoryRequestSchema>;

/** The reservation half, which is what capacity is sized against. */
export function resourceRequest(
  value: CpuRequest | MemoryRequest | null | undefined,
): string | number | null {
  if (value == null) return null;
  return Array.isArray(value) ? value[0] : value;
}

/** The ceiling half, present only when its author named one. */
export function resourceLimit(
  value: CpuRequest | MemoryRequest | null | undefined,
): string | number | null {
  return Array.isArray(value) ? value[1] : null;
}

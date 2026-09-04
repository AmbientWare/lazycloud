import type { GpuTypeEntitlement, PlanEntitlements } from "@/lib/api/schemas";
import { countLabel } from "@/lib/format";

/**
 * Plan terms as the words a customer reads them in.
 *
 * Every figure here arrives from the pricing catalog or the billing summary, so
 * nothing in this file knows which plan it is describing. What it owns is the
 * wording: one ceiling said the same way on the pricing page, in the plan
 * dialog, and in account settings, so a limit a customer met in one place is
 * recognisable in the next.
 */

/** A ceiling with no ceiling on it. Kept in one place so the word never varies. */
const UNLIMITED = "Unlimited";

/** A ceiling as the bare figure a comparison column shows: `30`, or `Unlimited`. */
export function limitFigure(limit: PlanEntitlements["max_workspaces"]): string {
  return limit === "unlimited" ? UNLIMITED : limit.toLocaleString();
}

/** A ceiling with the thing it bounds: `3 workspaces`, `Unlimited workspaces`. */
export function limitPhrase(
  limit: PlanEntitlements["max_workspaces"],
  singular: string,
  plural = `${singular}s`,
): string {
  return limit === "unlimited" ? `${UNLIMITED} ${plural}` : countLabel(limit, singular, plural);
}

/**
 * The member ceiling, which at one is the account owner and nobody else.
 *
 * The count includes the owner's own membership, so a plan allowing one member
 * is a workspace somebody works in alone rather than a workspace with room for
 * one person in it. Said as a person rather than as a figure, because `1 member`
 * reads as a vacancy.
 */
export function memberLimitFigure(limit: PlanEntitlements["max_members"]): string {
  if (limit === "unlimited") return UNLIMITED;
  return limit === 1 ? "Just you" : limit.toLocaleString();
}

export function memberLimitPhrase(limit: PlanEntitlements["max_members"]): string {
  if (limit === "unlimited") return `${UNLIMITED} members`;
  return limit === 1 ? "Just you" : countLabel(limit, "member");
}

/** The GPU models a plan may ask for, from the plan's own set rather than a list held here. */
export function gpuModelsLabel(gpuTypes: GpuTypeEntitlement): string {
  return gpuTypes === "all" ? "Every model" : gpuTypes.join(", ");
}

/** The same set as a line of its own, where no column heading says what it is. */
export function gpuModelsPhrase(gpuTypes: GpuTypeEntitlement): string {
  return gpuTypes === "all" ? "Every GPU model the platform rents" : `${gpuTypes.join(", ")} GPUs`;
}

/**
 * A live count against the ceiling holding it: `3 of 30 CPU containers`.
 *
 * The pair rather than the ceiling alone, because the ceiling belongs to the
 * account and what fills it may be in a workspace nobody is looking at. A limit
 * with no position reads as arbitrary the moment somebody is refused.
 */
export function usagePhrase(
  used: number,
  limit: PlanEntitlements["max_members"],
  singular: string,
  plural = `${singular}s`,
): string {
  if (limit === "unlimited") return countLabel(used, singular, plural);
  return `${used.toLocaleString()} of ${countLabel(limit, singular, plural)}`;
}

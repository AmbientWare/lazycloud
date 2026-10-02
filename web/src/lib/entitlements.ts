import { countLabel } from "@/lib/format";

/**
 * Plan terms as the words a customer reads them in.
 *
 * Every figure arrives from the pricing catalog or the billing account; this
 * file owns the wording, so one ceiling reads the same on the pricing page, in
 * the plan dialog and in account settings. An absent ceiling is unlimited.
 */

type Ceiling = number | undefined;

const UNLIMITED = "Unlimited";

/** A ceiling as the bare figure a comparison column shows: `30`, or `Unlimited`. */
export function limitFigure(limit: Ceiling): string {
  return limit === undefined ? UNLIMITED : limit.toLocaleString();
}

/** A ceiling with the thing it bounds: `3 workspaces`, `Unlimited workspaces`. */
export function limitPhrase(limit: Ceiling, singular: string, plural = `${singular}s`): string {
  return limit === undefined ? `${UNLIMITED} ${plural}` : countLabel(limit, singular, plural);
}

/**
 * The member ceiling counts the owner, so a limit of one is a workspace somebody
 * works in alone, said as a person because `1 member` reads as a vacancy.
 */
export function memberLimitFigure(limit: Ceiling): string {
  if (limit === undefined) return UNLIMITED;
  return limit === 1 ? "Just you" : limit.toLocaleString();
}

export function memberLimitPhrase(limit: Ceiling): string {
  if (limit === undefined) return `${UNLIMITED} members`;
  return limit === 1 ? "Just you" : countLabel(limit, "member");
}

/** Declared disk size per workspace, in the largest unit that states it whole: `1 TiB`. */
export function diskAllowanceFigure(gib: number): string {
  if (gib === 0) return "Not included";
  return gib % 1024 === 0 ? `${(gib / 1024).toLocaleString()} TiB` : `${gib.toLocaleString()} GiB`;
}

/** Whether a plan grants every GPU model the catalog rents. */
function everyModel(granted: readonly string[], offered: readonly string[]): boolean {
  return offered.length > 0 && offered.every((model) => granted.includes(model));
}

/** The GPU models a plan may ask for, against the models the catalog offers. */
export function gpuModelsLabel(granted: readonly string[], offered: readonly string[]): string {
  return everyModel(granted, offered) ? "All available models" : granted.join(", ");
}

/** The same set as a line of its own, where no column heading says what it is. */
export function gpuModelsPhrase(granted: readonly string[], offered: readonly string[]): string {
  return everyModel(granted, offered) ? "All available GPU models" : `${granted.join(", ")} GPUs`;
}

/** A live count against its ceiling: `3 of 30 CPU containers`. */
export function usagePhrase(
  used: number,
  limit: Ceiling,
  singular: string,
  plural = `${singular}s`,
): string {
  if (limit === undefined) return countLabel(used, singular, plural);
  return `${used.toLocaleString()} of ${countLabel(limit, singular, plural)}`;
}

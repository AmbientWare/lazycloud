const topLevelDestinations = new Set(["apps", "tasks", "storage", "usage"]);

/** Workspace switches keep the current product area, but never carry scoped resource ids. */
export function workspaceLandingPath(
  pathname: string,
  currentBase: string,
  nextBase: string,
): string {
  const relative = pathname.startsWith(currentBase) ? pathname.slice(currentBase.length) : "";
  const segment = relative.split("/").filter(Boolean)[0] ?? "apps";
  if (!topLevelDestinations.has(segment)) return `${nextBase}/apps`;
  return `${nextBase}/${segment}`;
}

export type ShellBreadcrumb = {
  label: string;
  href?: string;
};

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

const SECTION_LABELS: Record<string, string> = {
  apps: "Apps",
  tasks: "Tasks",
  storage: "Storage",
  usage: "Usage",
  sandboxes: "Sandboxes",
};

/**
 * Every ancestor as a link, then the current resource as plain text.
 *
 * One rule for every route, so the bar is in the same place saying the same kind
 * of thing wherever you are. The last crumb is deliberately the label the page's
 * own heading uses: a trail ending in something the page does not call itself
 * reads as a different resource.
 */
export function shellBreadcrumbs(
  pathname: string,
  workspaceName: string,
  appName?: string,
  sandboxName?: string,
): ShellBreadcrumb[] {
  const base = `/w/${encodeURIComponent(workspaceName)}`;
  const parts = pathname.slice(base.length).split("/").filter(Boolean).map(safeDecode);
  const [section = "apps", resourceId, childSection, childId] = parts;
  const sectionLabel = SECTION_LABELS[section] ?? section;
  const sectionHref = `${base}/${section}`;

  if (section !== "apps" && section !== "sandboxes") {
    return resourceId
      ? [{ label: sectionLabel, href: sectionHref }, { label: resourceId }]
      : [{ label: sectionLabel }];
  }

  // A sandbox is reached through the app that owns it, which is also the only
  // place the dashboard links to one — there is no sandboxes index to return to.
  if (section === "sandboxes") {
    return resourceId
      ? [{ label: "Apps", href: `${base}/apps` }, { label: sandboxName || resourceId }]
      : [{ label: "Apps", href: `${base}/apps` }];
  }

  if (!resourceId) return [{ label: "Apps" }];

  const appLabel = appName || resourceId;
  const appHref = `${base}/apps/${encodeURIComponent(resourceId)}`;
  const trail: ShellBreadcrumb[] = [{ label: "Apps", href: `${base}/apps` }];

  if (!childSection) return [...trail, { label: appLabel }];
  if (childSection === "tasks") {
    return [...trail, { label: appLabel, href: appHref }, { label: childId ?? "Task" }];
  }
  if (childSection !== "workloads" || !childId) {
    return [...trail, { label: appLabel, href: appHref }];
  }

  const workloadHref = `${appHref}/workloads/${encodeURIComponent(childId)}`;
  const grandId = parts[5];
  const workload: ShellBreadcrumb = grandId
    ? { label: childId, href: workloadHref }
    : { label: childId };
  const trailToWorkload = [...trail, { label: appLabel, href: appHref }, workload];
  return grandId ? [...trailToWorkload, { label: grandId }] : trailToWorkload;
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

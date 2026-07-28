export type ShellBreadcrumb = {
  label: string;
  href?: string;
};

const topLevelDestinations = new Set(["apps", "tasks", "storage", "usage", "settings"]);

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

export function shellBreadcrumbs(
  pathname: string,
  workspaceName: string,
  appName?: string,
): ShellBreadcrumb[] {
  const base = `/w/${encodeURIComponent(workspaceName)}`;
  const parts = pathname.slice(base.length).split("/").filter(Boolean).map(safeDecode);
  const [section, resourceId, childSection, childId] = parts;

  if (!section || section === "apps") {
    if (!resourceId) return [{ label: "Apps" }];

    const appLabel = appName || resourceId;
    const appHref = `${base}/apps/${encodeURIComponent(resourceId)}`;
    if (childSection === "workloads" && childId) {
      return [
        { label: "Apps", href: `${base}/apps` },
        { label: appLabel, href: appHref },
        { label: childId },
      ];
    }
    if (childSection === "tasks" && childId) {
      return [{ label: appLabel }];
    }
    return [];
  }

  if (section === "tasks") {
    return [{ label: "Tasks" }];
  }

  if (section === "sandboxes") {
    return resourceId
      ? [{ label: "Apps", href: `${base}/apps` }, { label: "Sandbox" }, { label: resourceId }]
      : [{ label: "Apps", href: `${base}/apps` }, { label: "Sandboxes" }];
  }

  const sectionLabels: Record<string, string> = {
    storage: "Storage",
    usage: "Usage",
    settings: "Settings",
  };
  return [{ label: sectionLabels[section] ?? section }];
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

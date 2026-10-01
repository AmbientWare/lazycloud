export type TaskListKeyParts = {
  mode: "page" | "infinite";
  limit: number;
  status: string | null;
  deploymentId: string | null;
  appId: string | null;
  stubIds: string | null;
  kind: string | null;
  createdAfter: string | null;
  createdBefore: string | null;
  createdWithinSeconds: number | null;
  search: string | null;
  rootOnly: boolean;
};

export type AccountActivityKeyParts = {
  measure: string;
  range: string;
  limit: number;
};

export type AccountCostKeyParts = {
  start: string;
  end: string;
  groupBy: string;
  appId: string | null;
  workspaceId: string | null;
  category: string | null;
};

export type AccountCostSeriesKeyParts = {
  start: string;
  end: string;
  bucket: string;
};

const workspaceRoot = (workspaceId: string) => ["workspace", workspaceId] as const;

export const workspaceQueryKeys = {
  root: workspaceRoot,
  apps: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "apps"] as const,
    summaries: (workspaceId: string) =>
      [...workspaceRoot(workspaceId), "apps", "summaries"] as const,
    details: (workspaceId: string) => [...workspaceRoot(workspaceId), "apps", "detail"] as const,
    detail: (workspaceId: string, appId: string) =>
      [...workspaceRoot(workspaceId), "apps", "detail", appId] as const,
    deploymentManifest: (workspaceId: string, deploymentId: string) =>
      [...workspaceRoot(workspaceId), "apps", "deployment-manifest", deploymentId] as const,
    deploymentUrl: (workspaceId: string, deploymentId: string) =>
      [...workspaceRoot(workspaceId), "apps", "deployment-url", deploymentId] as const,
  },
  members: (workspaceId: string) => [...workspaceRoot(workspaceId), "members"] as const,
  invitations: (workspaceId: string) => [...workspaceRoot(workspaceId), "invitations"] as const,
  deployments: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "deployments"] as const,
    list: (
      workspaceId: string,
      options: { limit: number; appId: string | null; name: string | null; kind: string | null },
    ) => [...workspaceRoot(workspaceId), "deployments", "list", options] as const,
    devbox: (workspaceId: string, deploymentId: string) =>
      [...workspaceRoot(workspaceId), "deployments", "devbox", deploymentId] as const,
  },
  workloads: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "workloads"] as const,
    list: (workspaceId: string, appId: string | null) =>
      [...workspaceRoot(workspaceId), "workloads", "list", { appId }] as const,
    cron: (workspaceId: string) => [...workspaceRoot(workspaceId), "workloads", "cron"] as const,
  },
  tasks: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "tasks"] as const,
    lists: (workspaceId: string) => [...workspaceRoot(workspaceId), "tasks", "list"] as const,
    list: (workspaceId: string, options: TaskListKeyParts) =>
      [...workspaceRoot(workspaceId), "tasks", "list", options] as const,
    details: (workspaceId: string) => [...workspaceRoot(workspaceId), "tasks", "detail"] as const,
    detail: (workspaceId: string, taskId: string) =>
      [...workspaceRoot(workspaceId), "tasks", "detail", taskId] as const,
    callGraphs: (workspaceId: string) =>
      [...workspaceRoot(workspaceId), "tasks", "call-graph"] as const,
    callGraph: (workspaceId: string, rootTaskId: string) =>
      [...workspaceRoot(workspaceId), "tasks", "call-graph", rootTaskId] as const,
    aggregates: (workspaceId: string) =>
      [...workspaceRoot(workspaceId), "tasks", "aggregate"] as const,
    metrics: (workspaceId: string, hours: number, appId: string | null) =>
      [...workspaceRoot(workspaceId), "tasks", "aggregate", "metrics", hours, appId] as const,
    buckets: (
      workspaceId: string,
      windowSeconds: number,
      appId: string | null,
      stubId: string | null,
    ) =>
      [
        ...workspaceRoot(workspaceId),
        "tasks",
        "aggregate",
        "buckets",
        windowSeconds,
        appId,
        stubId,
      ] as const,
    latency: (
      workspaceId: string,
      stubIds: string,
      deploymentId: string | null,
      windowSeconds: number,
    ) =>
      [
        ...workspaceRoot(workspaceId),
        "tasks",
        "aggregate",
        "latency",
        stubIds,
        deploymentId,
        windowSeconds,
      ] as const,
  },
  containers: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "containers"] as const,
    lists: (workspaceId: string) => [...workspaceRoot(workspaceId), "containers", "list"] as const,
    list: (
      workspaceId: string,
      options: { appId: string | null; stubIds: string | null; statuses: string | null },
    ) => [...workspaceRoot(workspaceId), "containers", "list", options] as const,
    details: (workspaceId: string) =>
      [...workspaceRoot(workspaceId), "containers", "detail"] as const,
    detail: (workspaceId: string, containerId: string) =>
      [...workspaceRoot(workspaceId), "containers", "detail", containerId] as const,
    eventSummaries: (workspaceId: string) =>
      [...workspaceRoot(workspaceId), "containers", "event-summary"] as const,
    eventSummary: (workspaceId: string, containerId: string) =>
      [...workspaceRoot(workspaceId), "containers", "event-summary", containerId] as const,
    metrics: (workspaceId: string, containerId: string) =>
      [...workspaceRoot(workspaceId), "containers", "metrics", containerId] as const,
    files: (workspaceId: string, containerId: string, path?: string) =>
      [
        ...workspaceRoot(workspaceId),
        "containers",
        "files",
        containerId,
        ...(path ? [path] : []),
      ] as const,
    // Outside "containers" so container list refreshes never refetch a preview.
    filePreview: (workspaceId: string, containerId: string, path: string, maxBytes: number) =>
      [
        ...workspaceRoot(workspaceId),
        "container-file-previews",
        containerId,
        path,
        maxBytes,
      ] as const,
  },
  sandboxes: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "sandboxes"] as const,
    list: (workspaceId: string, limit: number, appId: string | null) =>
      [...workspaceRoot(workspaceId), "sandboxes", "list", { limit, appId }] as const,
    processes: (workspaceId: string, containerId: string) =>
      [...workspaceRoot(workspaceId), "sandboxes", "processes", containerId] as const,
    urls: (workspaceId: string, containerId: string) =>
      [...workspaceRoot(workspaceId), "sandboxes", "urls", containerId] as const,
  },
  compute: {
    summary: (workspaceId: string) =>
      [...workspaceRoot(workspaceId), "compute", "summary"] as const,
  },
  storage: {
    root: (workspace: string) => [...workspaceRoot(workspace), "storage"] as const,
    artifacts: (workspace: string) =>
      [...workspaceRoot(workspace), "storage", "artifacts"] as const,
    secrets: (workspace: string) => [...workspaceRoot(workspace), "storage", "secrets"] as const,
    volumes: (workspace: string) => [...workspaceRoot(workspace), "storage", "volumes"] as const,
    disks: (workspace: string) => [...workspaceRoot(workspace), "storage", "disks"] as const,
    volumePath: (workspace: string, volume: string, path?: string) =>
      [
        ...workspaceRoot(workspace),
        "storage",
        "volume-path",
        volume,
        ...(path ? [path] : []),
      ] as const,
  },
  collections: {
    list: (workspace: string, kind: "maps" | "queues") =>
      [...workspaceRoot(workspace), "collections", kind] as const,
    resource: (workspace: string, kind: "map" | "queue", name: string) =>
      [...workspaceRoot(workspace), "collections", kind, name] as const,
    queue: (workspace: string, name: string) =>
      [...workspaceRoot(workspace), "collections", "queue", name, "info"] as const,
    queueHead: (workspace: string, name: string) =>
      [...workspaceRoot(workspace), "collections", "queue", name, "head"] as const,
    map: (workspace: string, name: string) =>
      [...workspaceRoot(workspace), "collections", "map", name, "info"] as const,
    mapKeys: (workspace: string, name: string) =>
      [...workspaceRoot(workspace), "collections", "map", name, "keys"] as const,
    mapValue: (workspace: string, name: string, key: string | null) =>
      [...workspaceRoot(workspace), "collections", "map", name, "value", key] as const,
  },
  logs: {
    history: (
      workspaceId: string,
      scope: {
        appId: string | null;
        stubId: string | null;
        taskId: string | null;
        containerId: string | null;
      },
    ) => [...workspaceRoot(workspaceId), "logs", "history", scope] as const,
  },
} as const;

export type AdminAccountsKeyParts = {
  search: string;
  role: string | null;
  status: string | null;
};

/** The unnarrowed list, and the key every mutation patches. */
export const EVERY_ACCOUNT: AdminAccountsKeyParts = { search: "", role: null, status: null };

const accountRoot = ["account"] as const;

/**
 * Keys for records a person owns rather than a workspace.
 *
 * Deliberately outside `workspaceRoot`: the connected cloud, its instances, the
 * joined machines, the registered domains, and the access tokens answer the same
 * in every workspace the account holds. Keying them per workspace would cache one
 * answer N times and refetch all of it on a switch that cannot have changed it.
 */
export const accountQueryKeys = {
  root: () => accountRoot,
  compute: {
    root: () => [...accountRoot, "compute"] as const,
    awsConnection: () => [...accountRoot, "compute", "aws-connection"] as const,
    instances: () => [...accountRoot, "compute", "instances"] as const,
    machines: () => [...accountRoot, "compute", "machines"] as const,
  },
  billing: () => [...accountRoot, "billing"] as const,
  metrics: {
    root: () => [...accountRoot, "metrics"] as const,
    containerCounts: () => [...accountRoot, "metrics", "container-counts"] as const,
    activity: (scope: AccountActivityKeyParts) =>
      [...accountRoot, "metrics", "activity", scope] as const,
  },
  /**
   * Spend is invoiced to the account, so it is cached against the account and
   * not against whichever workspace happened to be selected when it was read.
   * Kept apart from `billing`, which answers the plan: a plan change should not
   * refetch two windowed aggregates over the ledger.
   */
  usage: {
    root: () => [...accountRoot, "usage"] as const,
    costs: (scope: AccountCostKeyParts) => [...accountRoot, "usage", "costs", scope] as const,
    series: (scope: AccountCostSeriesKeyParts) =>
      [...accountRoot, "usage", "cost-series", scope] as const,
  },
  domains: () => [...accountRoot, "custom-domains"] as const,
  tokens: () => [...accountRoot, "tokens"] as const,
  /** Keyed on the link's own secret, so two open invitations never share a cache entry. */
  invitation: (token: string) => [...accountRoot, "invitation", token] as const,
  /**
   * What an administrator sees of every account on the platform. Under the
   * account root because who may read it is decided by the signed-in person,
   * not by the workspace in the address bar, and a switch cannot change it.
   */
  admin: {
    root: () => [...accountRoot, "admin"] as const,
    fleet: {
      root: () => [...accountRoot, "admin", "fleet"] as const,
      summary: () => [...accountRoot, "admin", "fleet", "summary"] as const,
      nodes: () => [...accountRoot, "admin", "fleet", "nodes"] as const,
    },
    // Keyed on the narrowing, so each search and filter caches its own pages
    // and changing one starts a fresh walk rather than appending to the last.
    accounts: Object.assign(
      (scope: AdminAccountsKeyParts = EVERY_ACCOUNT) =>
        [...accountRoot, "admin", "accounts", scope] as const,
      { root: () => [...accountRoot, "admin", "accounts"] as const },
    ),
  },
} as const;

export type WorkspaceLiveQueryMeta = {
  workspaceLiveEnabled: boolean;
  workspaceLiveCritical: boolean;
  workspaceLiveRecoverErrors: boolean;
};

export function workspaceLiveQueryMeta(
  critical: boolean,
  enabled = true,
  recoverErrors = true,
): WorkspaceLiveQueryMeta {
  return {
    workspaceLiveEnabled: enabled,
    workspaceLiveCritical: critical,
    workspaceLiveRecoverErrors: recoverErrors,
  };
}

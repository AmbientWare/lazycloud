import type { Schemas } from "@/lib/api/client";
import type { LogSource } from "@/lib/api/logs";

/** A workload's address: /apps/{app}/workloads/{kind}/{name}. */
export type WorkloadRef = Pick<Schemas["Workload"], "app" | "kind" | "name">;

export type TaskListKeyParts = {
  mode: "page" | "infinite";
  limit: number;
  app: string | null;
  function: string | null;
  status: string | null;
  version: number | null;
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

const workloadKey = (workspace: string, { app, kind, name }: WorkloadRef) =>
  [...workspaceRoot(workspace), "workloads", "detail", app, kind, name] as const;

export const workspaceQueryKeys = {
  root: workspaceRoot,
  apps: {
    root: (workspace: string) => [...workspaceRoot(workspace), "apps"] as const,
    list: (workspace: string) => [...workspaceRoot(workspace), "apps", "list"] as const,
    summaries: (workspace: string) => [...workspaceRoot(workspace), "apps", "summaries"] as const,
    detail: (workspace: string, app: string) =>
      [...workspaceRoot(workspace), "apps", "detail", app] as const,
    activities: (workspace: string) => [...workspaceRoot(workspace), "apps", "activity"] as const,
    activity: (workspace: string, app: string) =>
      [...workspaceRoot(workspace), "apps", "activity", app] as const,
  },
  search: (workspaceName: string, group: "apps" | "workloads" | "tasks", term: string) =>
    [...workspaceRoot(workspaceName), "search", group, term] as const,
  members: (workspaceId: string) => [...workspaceRoot(workspaceId), "members"] as const,
  invitations: (workspaceId: string) => [...workspaceRoot(workspaceId), "invitations"] as const,
  workloads: {
    root: (workspace: string) => [...workspaceRoot(workspace), "workloads"] as const,
    list: (workspace: string, app: string | null) =>
      [...workspaceRoot(workspace), "workloads", "list", { app }] as const,
    detail: workloadKey,
    versions: (workspace: string, workload: WorkloadRef) =>
      [...workloadKey(workspace, workload), "versions"] as const,
    devbox: (workspace: string, { app, name }: Pick<WorkloadRef, "app" | "name">) =>
      [...workloadKey(workspace, { app, kind: "pod", name }), "devbox"] as const,
    performance: (workspace: string, workload: WorkloadRef, windowSeconds: number) =>
      [...workloadKey(workspace, workload), "performance", windowSeconds] as const,
  },
  tasks: {
    root: (workspace: string) => [...workspaceRoot(workspace), "tasks"] as const,
    lists: (workspace: string) => [...workspaceRoot(workspace), "tasks", "list"] as const,
    list: (workspace: string, options: TaskListKeyParts) =>
      [...workspaceRoot(workspace), "tasks", "list", options] as const,
    // Under the task lists, so the changes that refresh those refresh these.
    requests: (workspace: string, app: string, name: string, limit: number) =>
      [...workspaceRoot(workspace), "tasks", "list", "requests", { app, name, limit }] as const,
    details: (workspace: string) => [...workspaceRoot(workspace), "tasks", "detail"] as const,
    detail: (workspace: string, taskId: string) =>
      [...workspaceRoot(workspace), "tasks", "detail", taskId] as const,
    request: (workspace: string, requestId: string) =>
      [...workspaceRoot(workspace), "tasks", "detail", "request", requestId] as const,
    callGraphs: (workspace: string) =>
      [...workspaceRoot(workspace), "tasks", "call-graph"] as const,
    callGraph: (workspace: string, rootTaskId: string) =>
      [...workspaceRoot(workspace), "tasks", "call-graph", rootTaskId] as const,
    aggregates: (workspace: string) => [...workspaceRoot(workspace), "tasks", "aggregate"] as const,
    metrics: (workspace: string, hours: number, app: string | null) =>
      [...workspaceRoot(workspace), "tasks", "aggregate", "metrics", hours, app] as const,
  },
  containers: {
    root: (workspace: string) => [...workspaceRoot(workspace), "containers"] as const,
    lists: (workspace: string) => [...workspaceRoot(workspace), "containers", "list"] as const,
    list: (workspace: string, { app, kind, name }: WorkloadRef, live: boolean) =>
      [...workspaceRoot(workspace), "containers", "list", { app, kind, name, live }] as const,
    details: (workspace: string) => [...workspaceRoot(workspace), "containers", "detail"] as const,
    detail: (workspace: string, containerId: string) =>
      [...workspaceRoot(workspace), "containers", "detail", containerId] as const,
    lifecycle: (workspace: string, containerId: string) =>
      [...workspaceRoot(workspace), "containers", "lifecycle", containerId] as const,
    metrics: (workspace: string, containerId: string) =>
      [...workspaceRoot(workspace), "containers", "metrics", containerId] as const,
    files: (workspace: string, containerId: string, path?: string) =>
      [
        ...workspaceRoot(workspace),
        "containers",
        "files",
        containerId,
        ...(path ? [path] : []),
      ] as const,
    // Outside "containers" so container list refreshes never refetch a preview.
    filePreview: (workspace: string, containerId: string, path: string, maxBytes: number) =>
      [
        ...workspaceRoot(workspace),
        "container-file-previews",
        containerId,
        path,
        maxBytes,
      ] as const,
  },
  sandboxes: {
    // Under the container lists, so the container changes that refresh those refresh these.
    list: (workspace: string, app: string, limit: number) =>
      [...workspaceRoot(workspace), "containers", "list", "sandboxes", { app, limit }] as const,
    stats: (workspace: string, app: string) =>
      [...workspaceRoot(workspace), "containers", "list", "sandbox-stats", { app }] as const,
    processes: (workspace: string, containerId: string) =>
      [...workspaceRoot(workspace), "sandboxes", "processes", containerId] as const,
    ports: (workspace: string, containerId: string) =>
      [...workspaceRoot(workspace), "sandboxes", "ports", containerId] as const,
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
    history: (workspaceId: string, source: LogSource) =>
      [...workspaceRoot(workspaceId), "logs", "history", source] as const,
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
    summary: () => [...accountRoot, "metrics", "summary"] as const,
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

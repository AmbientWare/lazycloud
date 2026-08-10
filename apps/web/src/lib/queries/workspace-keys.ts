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
  deployments: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "deployments"] as const,
    list: (
      workspaceId: string,
      options: { limit: number; appId: string | null; name: string | null },
    ) => [...workspaceRoot(workspaceId), "deployments", "list", options] as const,
  },
  workloads: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "workloads"] as const,
    list: (workspaceId: string, appId: string | null) =>
      [...workspaceRoot(workspaceId), "workloads", "list", { appId }] as const,
    cron: (workspaceId: string) => [...workspaceRoot(workspaceId), "workloads", "cron"] as const,
    taskQueueState: (workspaceId: string, stubId: string) =>
      [...workspaceRoot(workspaceId), "workloads", "task-queue-state", stubId] as const,
  },
  tasks: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "tasks"] as const,
    lists: (workspaceId: string) => [...workspaceRoot(workspaceId), "tasks", "list"] as const,
    list: (workspaceId: string, options: TaskListKeyParts) =>
      [...workspaceRoot(workspaceId), "tasks", "list", options] as const,
    details: (workspaceId: string) => [...workspaceRoot(workspaceId), "tasks", "detail"] as const,
    detail: (workspaceId: string, taskId: string) =>
      [...workspaceRoot(workspaceId), "tasks", "detail", taskId] as const,
    artifacts: (workspaceId: string, taskId: string) =>
      [...workspaceRoot(workspaceId), "tasks", "artifacts", taskId] as const,
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
  },
  sandboxes: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "sandboxes"] as const,
    list: (workspaceId: string, limit: number, appId: string | null) =>
      [...workspaceRoot(workspaceId), "sandboxes", "list", { limit, appId }] as const,
    files: (workspaceId: string, containerId: string, path?: string) =>
      [
        ...workspaceRoot(workspaceId),
        "sandboxes",
        "files",
        containerId,
        ...(path ? [path] : []),
      ] as const,
    processes: (workspaceId: string, containerId: string) =>
      [...workspaceRoot(workspaceId), "sandboxes", "processes", containerId] as const,
    urls: (workspaceId: string, containerId: string) =>
      [...workspaceRoot(workspaceId), "sandboxes", "urls", containerId] as const,
  },
  storage: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "storage"] as const,
    secrets: (workspaceId: string) =>
      [...workspaceRoot(workspaceId), "storage", "secrets"] as const,
    volumes: (workspaceId: string) =>
      [...workspaceRoot(workspaceId), "storage", "volumes"] as const,
    volumePath: (workspaceId: string, volumeName: string, path?: string) =>
      [
        ...workspaceRoot(workspaceId),
        "storage",
        "volume-path",
        volumeName,
        ...(path ? [path] : []),
      ] as const,
  },
  collections: {
    queueSize: (workspaceId: string, name: string) =>
      [...workspaceRoot(workspaceId), "collections", "queue", name, "size"] as const,
    queuePeek: (workspaceId: string, name: string) =>
      [...workspaceRoot(workspaceId), "collections", "queue", name, "peek"] as const,
    mapCount: (workspaceId: string, name: string) =>
      [...workspaceRoot(workspaceId), "collections", "map", name, "count"] as const,
    mapKeys: (workspaceId: string, name: string) =>
      [...workspaceRoot(workspaceId), "collections", "map", name, "keys"] as const,
    mapValue: (workspaceId: string, name: string, key: string) =>
      [...workspaceRoot(workspaceId), "collections", "map", name, "value", key] as const,
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
  resources: {
    list: (workspaceId: string, configKey: string) =>
      [...workspaceRoot(workspaceId), "resources", configKey] as const,
  },
  usage: {
    root: (workspaceId: string) => [...workspaceRoot(workspaceId), "usage"] as const,
    overview: (
      workspaceId: string,
      window: {
        period: string | null;
        start: string | null;
        end: string | null;
      },
      bucketSeconds: number,
    ) => [...workspaceRoot(workspaceId), "usage", "overview", window, bucketSeconds] as const,
    workloads: (
      workspaceId: string,
      appId: string,
      start: string,
      end: string,
      bucketSeconds: number,
    ) =>
      [
        ...workspaceRoot(workspaceId),
        "usage",
        "workloads",
        appId,
        start,
        end,
        bucketSeconds,
      ] as const,
  },
  settings: {
    concurrency: (workspaceId: string) =>
      [...workspaceRoot(workspaceId), "settings", "concurrency"] as const,
  },
} as const;

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
    catalog: () => [...accountRoot, "compute", "catalog"] as const,
    awsConnection: () => [...accountRoot, "compute", "aws-connection"] as const,
    instances: () => [...accountRoot, "compute", "instances"] as const,
    machines: () => [...accountRoot, "compute", "machines"] as const,
  },
  domains: () => [...accountRoot, "custom-domains"] as const,
  tokens: () => [...accountRoot, "tokens"] as const,
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

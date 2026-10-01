import { queryOptions, type QueryClient } from "@tanstack/react-query";

import { ApiError, api, ok, type Schemas } from "@/lib/api/client";
import type { AppSummary, DeploymentManifest } from "@/lib/api/schemas";
import { clientContractSchema } from "@/lib/api/schemas/client_manifests";
import {
  parseDeploymentId,
  stubId,
  viewActiveDeployment,
  viewApp,
  viewStub,
} from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

import { appDirectory, workloadDirectory, workloadRelease } from "./directory";
import { countTotal, taskActivity } from "./tasks";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export function appQueryOptions(workspaceId: string, appId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.detail(workspaceId, appId),
    queryFn: async () =>
      viewApp(
        await ok(
          api.GET("/v1/workspaces/{workspace}/apps/{app}", {
            params: { path: { workspace: workspaceName(workspaceId), app: appId } },
          }),
        ),
        workspaceId,
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

/**
 * Every app with what its card shows. The API has no per-app summary, so the
 * latest workload, kinds and running containers come from the workspace's
 * deployed workloads and live containers, and 24 hour activity from one read
 * of the workspace's task activity.
 */
export function appSummariesQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.summaries(workspaceId),
    queryFn: async ({ client }) => {
      const workspace = workspaceName(workspaceId);
      const [apps, workloads, containers, activity] = await Promise.all([
        appDirectory(client, workspaceId, { fresh: true }),
        workloadDirectory(client, workspaceId, { fresh: true }),
        ok(
          api.GET("/v1/workspaces/{workspace}/containers", {
            params: { path: { workspace }, query: { live: true, limit: 1000 } },
          }),
        ),
        taskActivity(workspace, 3600),
      ]);
      const activityByApp = new Map(activity.series.map((series) => [series.app, series]));
      const items: AppSummary[] = [...apps.byId.values()]
        .sort((left, right) => left.name.localeCompare(right.name))
        .map((app) => {
          const deployed = [...workloads.byId.values()].filter((item) => item.app === app.name);
          const latest = deployed.sort((left, right) =>
            (right.deployed_at ?? right.created_at).localeCompare(
              left.deployed_at ?? left.created_at,
            ),
          )[0];
          const kinds: Record<string, number> = {};
          for (const workload of deployed) kinds[workload.kind] = (kinds[workload.kind] ?? 0) + 1;
          return {
            app: viewApp(app, workspaceId),
            latest_workload: latest ? viewStub(latest, workspaceId, app.id) : null,
            latest_deployment: latest ? viewActiveDeployment(latest, app.id) : null,
            workload_kinds: kinds,
            devbox_count: 0,
            workload_count: app.workloads,
            active_versions: deployed.filter((workload) => workload.state === "active").length,
            running_containers: containers.containers.filter(
              (container) =>
                container.app === app.name &&
                (container.state === "ready" || container.state === "draining"),
            ).length,
            ...activity24h(activityByApp.get(app.name)),
            last_deployed_at: latest?.deployed_at ?? null,
          };
        });
      return { items };
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

/** An app's tasks over the last 24 hours, in total and per hour by outcome. */
function activity24h(series: Schemas["ActivitySeries"] | undefined) {
  const buckets = series?.buckets ?? [];
  const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);
  const failures = buckets.map((bucket) => bucket.status_counts.failed);
  const pending = buckets.map(
    (bucket) => bucket.status_counts.queued + bucket.status_counts.running,
  );
  const succeeded = buckets.map((bucket) => bucket.status_counts.succeeded);
  return {
    runs_24h: series?.total ?? 0,
    failed_runs_24h: sum(failures),
    pending_runs_24h: sum(pending),
    succeeded_runs_24h: sum(succeeded),
    activity_24h: buckets.map((bucket) => countTotal(bucket.status_counts)),
    failures_24h: failures,
    pending_24h: pending,
    succeeded_24h: succeeded,
  };
}

export function invalidateAppLists(queryClient: QueryClient, workspaceId: string) {
  return Promise.all(
    [
      workspaceQueryKeys.apps.root(workspaceId),
      workspaceQueryKeys.deployments.root(workspaceId),
      workspaceQueryKeys.containers.root(workspaceId),
    ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
}

const appPath = (workspaceId: string, appId: string) => ({
  params: { path: { workspace: workspaceName(workspaceId), app: appId } },
});

export function pauseAppMutationOptions(workspaceId: string, appId: string) {
  return {
    mutationFn: async () =>
      viewApp(
        await ok(
          api.POST("/v1/workspaces/{workspace}/apps/{app}/pause", appPath(workspaceId, appId)),
        ),
        workspaceId,
      ),
  };
}

export function resumeAppMutationOptions(workspaceId: string, appId: string) {
  return {
    mutationFn: async () =>
      viewApp(
        await ok(
          api.POST("/v1/workspaces/{workspace}/apps/{app}/resume", appPath(workspaceId, appId)),
        ),
        workspaceId,
      ),
  };
}

export function deleteAppMutationOptions(workspaceId: string, appId: string) {
  return {
    mutationFn: async () => {
      await ok(api.DELETE("/v1/workspaces/{workspace}/apps/{app}", appPath(workspaceId, appId)));
      return null;
    },
  };
}

const deploymentPath = (workspaceId: string, deploymentId: string) => ({
  params: {
    path: {
      workspace: workspaceName(workspaceId),
      deployment: parseDeploymentId(deploymentId).workload,
    },
  },
});

/** Deletes the workloads the rows belong to, each once, with every version. */
export function deleteWorkloadMutationOptions(workspaceId: string, deploymentIds: string[]) {
  return {
    mutationFn: async () => {
      const workloads = new Set(deploymentIds.map((id) => parseDeploymentId(id).workload));
      for (const workload of workloads) {
        await ok(
          api.DELETE("/v1/workspaces/{workspace}/deployments/{deployment}", {
            params: { path: { workspace: workspaceName(workspaceId), deployment: workload } },
          }),
        );
      }
      return null;
    },
  };
}

/** Starts the workload on this row's version, making it the active one. */
export function startDeploymentMutationOptions(workspaceId: string, deploymentId: string) {
  const { version } = parseDeploymentId(deploymentId);
  return {
    mutationFn: () =>
      ok(
        api.POST("/v1/workspaces/{workspace}/deployments/{deployment}/start", {
          ...deploymentPath(workspaceId, deploymentId),
          body: version === null ? {} : { version },
        }),
      ),
  };
}

export function stopDeploymentMutationOptions(workspaceId: string, deploymentId: string) {
  return {
    mutationFn: () =>
      ok(
        api.POST(
          "/v1/workspaces/{workspace}/deployments/{deployment}/stop",
          deploymentPath(workspaceId, deploymentId),
        ),
      ),
  };
}

/** Pods scale through the workloads API, which no merged packet serves yet. */
export function scaleDeploymentMutationOptions(
  workspaceId: string,
  deploymentId: string,
  replicas: number,
) {
  void workspaceId;
  void deploymentId;
  void replicas;
  return {
    mutationFn: (): Promise<null> =>
      Promise.reject(
        new ApiError(404, "Not Found", JSON.stringify({ message: "no such operation" })),
      ),
  };
}

export function deleteDeploymentMutationOptions(workspaceId: string, deploymentId: string) {
  return {
    mutationFn: async () => {
      await ok(
        api.DELETE(
          "/v1/workspaces/{workspace}/deployments/{deployment}",
          deploymentPath(workspaceId, deploymentId),
        ),
      );
      return null;
    },
  };
}

/** The deployment's workload and its release at the deployment's version. */
async function deployedRelease(workspaceId: string, deploymentId: string) {
  const workspace = workspaceName(workspaceId);
  const { version } = parseDeploymentId(deploymentId);
  const workload = await ok(
    api.GET(
      "/v1/workspaces/{workspace}/deployments/{deployment}",
      deploymentPath(workspaceId, deploymentId),
    ),
  );
  const { release, http } = await workloadRelease(workspace, workload, version ?? undefined);
  const active = version === null || version === workload.version;
  return { workspace, workload, release, http, active, version: version ?? workload.version ?? 1 };
}

/** The function's HTTP invoke on this origin, at its active release or one version. */
function functionInvokePath(workspace: string, app: string, name: string, version?: number) {
  const path = `/v1/workspaces/${encodeURIComponent(workspace)}/apps/${encodeURIComponent(app)}/functions/${encodeURIComponent(name)}`;
  return version === undefined ? `${path}/invoke` : `${path}/versions/${version}/invoke`;
}

/**
 * Invoke manifest for one deployment: the callable contract the deploy
 * recorded, and where a call to this version goes. A function answers on this
 * origin's invoke operation; an endpoint or ASGI app on its own host.
 */
export function deploymentManifestQueryOptions(workspaceId: string, deploymentId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.deploymentManifest(workspaceId, deploymentId),
    queryFn: async (): Promise<DeploymentManifest> => {
      const { workspace, workload, release, http, version } = await deployedRelease(
        workspaceId,
        deploymentId,
      );
      const { spec } = release;
      const invokePath = http
        ? http.version_url
        : functionInvokePath(workspace, workload.app, workload.name, version);
      const contract = clientContractSchema.safeParse(spec.client_contract);
      return {
        app: workload.app,
        name: workload.name,
        kind: workload.kind,
        stub_id: stubId(workload.app, workload.name, release.id),
        deployment_id: deploymentId,
        deployment_version: version,
        invoke_url: http ? http.version_url : `${window.location.origin}${invokePath}`,
        invoke_path: invokePath,
        timeout_seconds: spec.timeout_seconds ?? null,
        methods: spec.http?.methods ?? [],
        inputs: { fields: {} },
        client_contract: contract.success ? contract.data : null,
      };
    },
  });
}

/** Where the deployment answers HTTP: following the active release, or pinned to its version. */
export function deploymentUrlQueryOptions(workspaceId: string, deploymentId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.deploymentUrl(workspaceId, deploymentId),
    queryFn: async (): Promise<{ url: string }> => {
      const { workspace, workload, http, active, version } = await deployedRelease(
        workspaceId,
        deploymentId,
      );
      if (http) return { url: active ? http.url : http.version_url };
      const path = functionInvokePath(
        workspace,
        workload.app,
        workload.name,
        active ? undefined : version,
      );
      return {
        url: `${window.location.origin}${path}`,
      };
    },
  });
}

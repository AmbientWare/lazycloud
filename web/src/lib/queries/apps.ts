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

import { appDirectory, workloadDirectory } from "./directory";
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
      await client.invalidateQueries({
        queryKey: [...workspaceQueryKeys.apps.root(workspaceId), "directory"],
        refetchType: "none",
      });
      await client.invalidateQueries({
        queryKey: [...workspaceQueryKeys.deployments.root(workspaceId), "directory"],
        refetchType: "none",
      });
      const [apps, workloads, containers, activity] = await Promise.all([
        appDirectory(client, workspaceId),
        workloadDirectory(client, workspaceId),
        ok(
          api.GET("/v1/workspaces/{workspace}/containers", {
            params: { path: { workspace }, query: { live: true, limit: 1000 } },
          }),
        ),
        taskActivity(workspaceId, 3600),
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

async function deployedFunction(workspaceId: string, deploymentId: string) {
  const workspace = workspaceName(workspaceId);
  const workload = await ok(
    api.GET(
      "/v1/workspaces/{workspace}/deployments/{deployment}",
      deploymentPath(workspaceId, deploymentId),
    ),
  );
  const fn = await ok(
    api.GET("/v1/workspaces/{workspace}/apps/{app}/functions/{function}", {
      params: { path: { workspace, app: workload.app, function: workload.name } },
    }),
  );
  return { workspace, workload, fn };
}

/**
 * Invoke manifest for one deployment: the callable contract the deploy
 * recorded, and where a call goes. Functions are invoked by admitting a task
 * with JSON arguments until the HTTP invoke URL is served.
 */
export function deploymentManifestQueryOptions(workspaceId: string, deploymentId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.deploymentManifest(workspaceId, deploymentId),
    queryFn: async (): Promise<DeploymentManifest> => {
      const { workspace, workload, fn } = await deployedFunction(workspaceId, deploymentId);
      const spec = fn.active_release.spec;
      const contract = clientContractSchema.safeParse(spec.client_contract);
      return {
        app: workload.app,
        name: workload.name,
        kind: workload.kind,
        stub_id: stubId(workload.app, workload.name, fn.active_release.id),
        deployment_id: deploymentId,
        deployment_version: fn.active_release.version ?? workload.version ?? 1,
        invoke_url: "",
        invoke_path: `/v1/workspaces/${encodeURIComponent(workspace)}/apps/${encodeURIComponent(workload.app)}/functions/${encodeURIComponent(workload.name)}/invoke`,
        timeout_seconds: spec.timeout_seconds ?? null,
        methods: [],
        inputs: { fields: {} },
        client_contract: contract.success ? contract.data : null,
      };
    },
  });
}

/** The HTTP invoke URL; the endpoints packet serves it. */
export function deploymentUrlQueryOptions(workspaceId: string, deploymentId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.deploymentUrl(workspaceId, deploymentId),
    queryFn: (): Promise<{ url: string }> =>
      Promise.reject(
        new ApiError(
          404,
          "Not Found",
          JSON.stringify({ message: "Invoke URLs are not served yet" }),
        ),
      ),
    retry: false,
  });
}

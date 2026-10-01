import { queryOptions, type QueryClient } from "@tanstack/react-query";

import { ApiError, api, ok } from "@/lib/api/client";
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
 * deployed workloads and live containers; 24 hour activity waits for the
 * observability API and reads as none.
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
      const [apps, workloads, containers] = await Promise.all([
        appDirectory(client, workspaceId),
        workloadDirectory(client, workspaceId),
        ok(
          api.GET("/v1/workspaces/{workspace}/containers", {
            params: { path: { workspace }, query: { live: true, limit: 1000 } },
          }),
        ),
      ]);
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
            runs_24h: 0,
            failed_runs_24h: 0,
            pending_runs_24h: 0,
            succeeded_runs_24h: 0,
            activity_24h: [],
            failures_24h: [],
            pending_24h: [],
            succeeded_24h: [],
            last_deployed_at: latest?.deployed_at ?? null,
          };
        });
      return { items };
    },
    meta: workspaceLiveQueryMeta(true),
  });
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

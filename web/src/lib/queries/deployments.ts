import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, apiRequest, ok, postJson, withWorkspace } from "@/lib/api/client";
import {
  devboxSchema,
  type Deployment,
  type DeploymentList,
  type DevboxPhase,
} from "@/lib/api/schemas";
import { viewActiveDeployment, viewDeployment } from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

import { appById, appDirectory } from "./directory";

import {
  LIVE_LIST_MAX_PAGES,
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type DeploymentListOptions = {
  appId?: string;
  name?: string;
  kind?: string;
  limit?: number;
};

export function deploymentsInfiniteQueryOptions(
  workspaceId: string,
  options: DeploymentListOptions = {},
) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.deployments.list(workspaceId, {
      limit: options.limit ?? 100,
      appId: options.appId ?? null,
      name: options.name ?? null,
      kind: options.kind ?? null,
    }),
    initialPageParam: "",
    // One row per deployed version. A workload named on its own lists every
    // version; a wider list shows each workload's active version.
    queryFn: async ({ pageParam, client }): Promise<DeploymentList> => {
      const workspace = workspaceName(workspaceId);
      const app = options.appId ? await appById(client, workspaceId, options.appId) : undefined;
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/deployments", {
          params: {
            path: { workspace },
            query: {
              app: app?.name,
              name: options.name,
              limit: options.limit ?? 100,
              cursor: pageParam || undefined,
            },
          },
        }),
      );
      const workloads = page.deployments.filter(
        (workload) => !options.kind || workload.kind === options.kind,
      );
      const apps = app ? null : await appDirectory(client, workspaceId);
      const appId = (name: string) => app?.id ?? apps?.byName.get(name)?.id ?? "";
      if (!options.name) {
        const data = workloads.map((workload) =>
          viewActiveDeployment(workload, appId(workload.app)),
        );
        return { data, next: page.next_cursor ?? "" };
      }
      const data: Deployment[] = [];
      for (const workload of workloads) {
        const [versions, fn] = await Promise.all([
          ok(
            api.GET("/v1/workspaces/{workspace}/deployments/{deployment}/versions", {
              params: { path: { workspace, deployment: workload.id }, query: { limit: 1000 } },
            }),
          ),
          ok(
            api.GET("/v1/workspaces/{workspace}/apps/{app}/functions/{function}", {
              params: { path: { workspace, app: workload.app, function: workload.name } },
            }),
          ),
        ]);
        const id = appId(workload.app);
        for (const version of versions.versions) {
          data.push(
            viewDeployment(workload, id, version, {
              spec: fn.active_release.spec,
              onlyVersion: versions.versions.length === 1,
            }),
          );
        }
      }
      return { data, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
    meta: workspaceLiveQueryMeta(true),
  });
}

/**
 * A devbox's status, from the endpoint cheap enough to poll.
 *
 * Connections and the idle deadline change without a workspace event, so it
 * refreshes on an interval: quickly while the devbox is changing state or the
 * caller is waiting for a change it asked for, slowly once it has settled, and
 * not at all while the tab is hidden.
 */
export function devboxQueryOptions(
  workspaceId: string,
  deploymentId: string,
  { awaitingChange = false }: { awaitingChange?: boolean } = {},
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.deployments.devbox(workspaceId, deploymentId),
    queryFn: () =>
      apiRequest(
        withWorkspace(
          `/api/v1/deployments/${encodeURIComponent(deploymentId)}/devbox`,
          workspaceId,
        ),
        devboxSchema,
      ),
    refetchInterval: (query) =>
      !awaitingChange && query.state.data && SETTLED_PHASES.has(query.state.data.phase)
        ? SETTLED_REFRESH_MS
        : CHANGING_REFRESH_MS,
    meta: workspaceLiveQueryMeta(false),
  });
}

/** Ask a stopped devbox to start; the server answers with its status without waiting. */
export function startDevboxMutationOptions(workspaceId: string, deploymentId: string) {
  return {
    mutationFn: () =>
      postJson(
        withWorkspace(
          `/api/v1/deployments/${encodeURIComponent(deploymentId)}/devbox/start`,
          workspaceId,
        ),
        devboxSchema,
      ),
  };
}

/** Stop a devbox's container now; its deployment stays on. */
export function stopDevboxMutationOptions(workspaceId: string, deploymentId: string) {
  return {
    mutationFn: () =>
      postJson(
        withWorkspace(
          `/api/v1/deployments/${encodeURIComponent(deploymentId)}/devbox/stop`,
          workspaceId,
        ),
        devboxSchema,
      ),
  };
}

const SETTLED_PHASES = new Set<DevboxPhase>(["running", "stopped", "failed"]);
const SETTLED_REFRESH_MS = 15_000;
const CHANGING_REFRESH_MS = 3_000;

export function selectDeploymentList(
  data: InfiniteListQueryData<Deployment> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (deployment) => deployment.id);
}

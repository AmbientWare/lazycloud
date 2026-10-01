import { infiniteQueryOptions, mutationOptions, queryOptions } from "@tanstack/react-query";

import { api, apiRequest, ok, withWorkspace } from "@/lib/api/client";
import {
  containerMetricsTimeseriesSchema,
  type Container,
  type ContainerDetail,
  type ContainerWithAppPage,
} from "@/lib/api/schemas";
import {
  parseStubId,
  viewActiveDeployment,
  viewApp,
  viewContainer,
  viewStub,
} from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

import { appById, appDirectory, workloadDirectory } from "./directory";

import {
  LIVE_LIST_MAX_PAGES,
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type ContainerListOptions = {
  appId?: string;
  stubIds?: string[];
  statuses?: ContainerListStatus[];
  enabled?: boolean;
};

export type ContainerListStatus = "pending" | "running" | "exited" | "failed" | "stopped";

export function containersQueryOptions(workspaceId: string, options: ContainerListOptions = {}) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.containers.list(workspaceId, {
      appId: options.appId ?? null,
      stubIds: options.stubIds?.join(",") ?? null,
      statuses: options.statuses?.join(",") ?? null,
    }),
    initialPageParam: "",
    // The API lists the workspace's containers, or its live ones; the app,
    // workload and status narrowing happens here on that page.
    queryFn: async ({ pageParam, client }): Promise<ContainerWithAppPage> => {
      const live =
        options.statuses !== undefined &&
        options.statuses.every((status) => status === "pending" || status === "running");
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/containers", {
          params: {
            path: { workspace: workspaceName(workspaceId) },
            query: { live, limit: live ? 1000 : 100, cursor: pageParam || undefined },
          },
        }),
      );
      const apps = await appDirectory(client, workspaceId);
      const app = options.appId ? await appById(client, workspaceId, options.appId) : undefined;
      const workloads = new Set(
        (options.stubIds ?? []).map((id) => {
          const stub = parseStubId(id);
          return `${stub.app}/${stub.name}`;
        }),
      );
      const data = page.containers
        .filter((container) => !app || container.app === app.name)
        .filter(
          (container) => !workloads.size || workloads.has(`${container.app}/${container.function}`),
        )
        .map((container) => {
          const appId = apps.byName.get(container.app)?.id ?? "";
          return { container: viewContainer(container, workspaceId, appId), app_id: appId };
        })
        .filter(
          ({ container }) =>
            !options.statuses || options.statuses.includes(container.status as ContainerListStatus),
        );
      return { data, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
    enabled: options.enabled,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function selectContainerList(
  data: InfiniteListQueryData<ContainerWithAppPage["data"][number]> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (item) => item.container.id);
}

export function containerQueryOptions(workspaceId: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.detail(workspaceId, containerId),
    queryFn: async ({ client }): Promise<ContainerDetail> => {
      const container = await ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}", {
          params: { path: { workspace: workspaceName(workspaceId), container: containerId } },
        }),
      );
      const [apps, workloads] = await Promise.all([
        appDirectory(client, workspaceId),
        workloadDirectory(client, workspaceId),
      ]);
      const app = apps.byName.get(container.app);
      const workload = workloads.byName.get(`${container.app}/${container.function}`);
      const view = viewContainer(container, workspaceId, app?.id ?? null);
      return {
        ...view,
        app: app ? viewApp(app, workspaceId) : null,
        workload: workload ? viewStub(workload, workspaceId, app?.id ?? "") : null,
        deployment: workload ? viewActiveDeployment(workload, app?.id ?? "") : null,
        run_name: null,
        run_status: null,
        expires_at: null,
        actions: {
          can_stop: container.state !== "stopped",
          can_shell: false,
          can_create_image: false,
          can_snapshot_memory: false,
        },
      };
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

export function stopContainerMutationOptions(workspaceId: string, containerId: string) {
  return mutationOptions({
    mutationFn: async (): Promise<Container> =>
      viewContainer(
        await ok(
          api.POST("/v1/workspaces/{workspace}/containers/{container}/stop", {
            params: { path: { workspace: workspaceName(workspaceId), container: containerId } },
          }),
        ),
        workspaceId,
        null,
      ),
  });
}

export function containerMetricsTimeseriesQueryOptions(
  workspaceId: string,
  containerId: string,
  live: boolean,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.metrics(workspaceId, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/metrics/containers/${containerId}/timeseries`, workspaceId),
        containerMetricsTimeseriesSchema,
      ),
    refetchInterval: live ? 5_000 : false,
  });
}

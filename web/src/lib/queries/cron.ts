import { queryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";
import type { CronJob } from "@/lib/api/schemas";
import { deploymentId } from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

import { workloadDirectory } from "./directory";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/** Registered cron jobs (schedule expression, next/last run) for the workspace. */
export function cronJobsQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.workloads.cron(workspaceId),
    queryFn: async ({ client }): Promise<{ cron_jobs: CronJob[] }> => {
      const workspace = workspaceName(workspaceId);
      const [workloads, schedules] = await Promise.all([
        workloadDirectory(client, workspaceId, { fresh: true }),
        ok(
          api.GET("/v1/workspaces/{workspace}/schedules", {
            params: { path: { workspace }, query: { limit: 100 } },
          }),
        ),
      ]);
      const cron_jobs: CronJob[] = [];
      for (const { app, function: name, schedule } of schedules.schedules) {
        const workload = workloads.byName.get(`${app}/${name}`);
        if (!workload) continue;
        cron_jobs.push({
          name,
          cron: schedule.cron,
          deployment_id: deploymentId(workload.id, workload.version ?? 1),
          enabled: workload.state === "active",
          last_run_at: schedule.last_run_at ?? null,
          next_run_at: schedule.next_run_at,
        });
      }
      return { cron_jobs };
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

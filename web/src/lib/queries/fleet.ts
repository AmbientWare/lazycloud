import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import {
  fleetReleasePhaseSchema,
  type FleetNodeList,
  type FleetReleasePhase,
  type FleetSummary,
} from "@/lib/api/schemas";
import { nextListCursor } from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

export function fleetSummaryQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.admin.fleet.summary(),
    queryFn: async (): Promise<FleetSummary> => viewSummary(await ok(api.GET("/v1/fleet"))),
  });
}

export function fleetNodesQueryOptions() {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.admin.fleet.nodes(),
    initialPageParam: "",
    queryFn: async ({ pageParam }): Promise<FleetNodeList> => {
      const page = await ok(
        api.GET("/v1/fleet/nodes", {
          params: { query: { limit: 50, cursor: pageParam || undefined } },
        }),
      );
      return {
        data: page.nodes.map((node) => ({
          ...node,
          machine_id: node.machine_id ?? null,
          instance_id: node.instance_id ?? null,
        })),
        next: page.next_cursor ?? "",
        observed_at: page.observed_at,
      };
    },
    getNextPageParam: nextListCursor,
  });
}

const releasePhases: ReadonlySet<string> = new Set(fleetReleasePhaseSchema.options);

function viewSummary(summary: Schemas["FleetSummary"]): FleetSummary {
  const release = summary.release;
  const phases: Partial<Record<FleetReleasePhase, number>> = {};
  for (const [phase, count] of Object.entries(release?.phases ?? {})) {
    if (releasePhases.has(phase)) phases[phase as FleetReleasePhase] = count;
  }
  return {
    observed_at: summary.observed_at,
    plan: summary.plan ?? null,
    release: release ? { ...release, phases } : null,
  };
}

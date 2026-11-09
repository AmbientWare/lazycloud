import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

/** What the platform's own cloud hosts cost, refreshed each minute. */
export function fleetSpendQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.admin.spend(),
    queryFn: () => ok(api.GET("/v1/fleet/spend")),
    refetchInterval: 60_000,
  });
}

export function adminAlertsQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.admin.alerts(),
    queryFn: () => ok(api.GET("/v1/admin-alerts")),
  });
}

export function setAdminAlerts(alerts: Schemas["AdminAlerts"]) {
  return ok(api.PUT("/v1/admin-alerts", { body: alerts }));
}

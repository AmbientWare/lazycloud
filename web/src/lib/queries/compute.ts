import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { accountQueryKeys, workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/**
 * The change stream carries no compute topics, so these reads poll. The
 * account's capacity, connection and machines change slowly.
 */
const ACCOUNT_CAPACITY_POLL_INTERVAL_MS = 30_000;
/** While the server moves the connection between phases. */
const CONNECTION_TRANSITION_POLL_INTERVAL_MS = 5_000;
/**
 * While the customer creates the stack in the AWS Console, which the server
 * checks every 2 seconds for the setup's first 30 minutes.
 */
const CONNECTION_SETUP_POLL_INTERVAL_MS = 2_000;
const CONNECTION_SETUP_WINDOW_MS = 30 * 60_000;
/** While the join dialog waits for a host to come up. */
const FOLLOWED_MACHINE_POLL_INTERVAL_MS = 5_000;

/** Bounded single pages, as the settings lists read them. */
const MACHINE_PAGE_LIMIT = 250;
const INSTANCE_PAGE_LIMIT = 100;

/**
 * Machines this account joined itself, each serving the workspaces it names.
 * `following` names a machine whose join a dialog is watching; the list polls
 * faster until that machine is ready or failed.
 */
export function machinesQueryOptions(following?: string) {
  return queryOptions({
    queryKey: accountQueryKeys.compute.machines(),
    queryFn: () =>
      ok(api.GET("/v1/machines", { params: { query: { limit: MACHINE_PAGE_LIMIT } } })),
    refetchInterval: (query) => {
      const machine = following
        ? query.state.data?.machines.find(({ id }) => id === following)
        : undefined;
      return following && machine?.lifecycle !== "ready" && machine?.lifecycle !== "failed"
        ? FOLLOWED_MACHINE_POLL_INTERVAL_MS
        : ACCOUNT_CAPACITY_POLL_INTERVAL_MS;
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Machines the account's connected cloud launched, with their provider facts. */
export function connectionMachinesQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountQueryKeys.compute.instances(),
    enabled,
    queryFn: () =>
      ok(api.GET("/v1/compute/instances", { params: { query: { limit: INSTANCE_PAGE_LIMIT } } })),
    refetchInterval: ACCOUNT_CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

/** The connected cloud's counts and cost, as the server classifies them. */
export function computeSummaryQueryOptions(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.compute.summary(workspace),
    queryFn: () =>
      ok(api.GET("/v1/workspaces/{workspace}/compute", { params: { path: { workspace } } })),
    refetchInterval: ACCOUNT_CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function awsConnectionQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountQueryKeys.compute.awsConnection(),
    enabled,
    queryFn: async () => (await ok(api.GET("/v1/aws-connection"))).connection ?? null,
    refetchInterval: (query) => {
      const connection = query.state.data;
      if (!connection) return ACCOUNT_CAPACITY_POLL_INTERVAL_MS;
      const pending = connection.pending_authorization;
      if (
        pending?.setup_step &&
        !connection.active_authorization &&
        Date.now() - Date.parse(pending.created_at) < CONNECTION_SETUP_WINDOW_MS
      ) {
        return CONNECTION_SETUP_POLL_INTERVAL_MS;
      }
      return connectionIsTransitioning(connection)
        ? CONNECTION_TRANSITION_POLL_INTERVAL_MS
        : ACCOUNT_CAPACITY_POLL_INTERVAL_MS;
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

function connectionIsTransitioning(connection: Schemas["AwsConnection"]): boolean {
  switch (connection.phase) {
    case "awaiting_authorization":
    case "validating":
    case "reconnect_pending":
    case "retiring_authorization":
    case "disconnect_draining":
    case "revoking":
    case "verifying_revocation":
      return true;
    case "ready":
    case "degraded":
    case "action_required":
      return false;
  }
}

export function createAwsConnection(accountId: string) {
  return ok(api.POST("/v1/aws-connection", { body: { account_id: accountId } }));
}

export function validateAwsConnection() {
  return ok(api.POST("/v1/aws-connection/validate"));
}

export function reconnectAwsConnection() {
  return ok(api.POST("/v1/aws-connection/reconnect", { body: {} }));
}

/** The connection while its removal runs, or null once it is gone. */
export async function removeAwsConnection(): Promise<Schemas["AwsConnection"] | null> {
  return (await ok(api.DELETE("/v1/aws-connection"))).connection ?? null;
}

export function cancelAwsConnectionReconnect() {
  return ok(api.DELETE("/v1/aws-connection/reconnect"));
}

export function retryAwsConnection() {
  return ok(api.POST("/v1/aws-connection/retry"));
}

/** Removes a connection whose cleanup cannot finish; the result lists what stays in AWS. */
export function forgetAwsConnection() {
  return ok(api.POST("/v1/aws-connection/forget"));
}

export function createMachineJoinCommand(body: Schemas["MachineJoinRequest"]) {
  return ok(api.POST("/v1/machines/join-command", { body }));
}

export function updateMachineWorkspaces(machineId: string, workspaces: string[]) {
  return ok(
    api.PATCH("/v1/machines/{machine}", {
      params: { path: { machine: machineId } },
      body: { workspaces },
    }),
  );
}

import { queryOptions } from "@tanstack/react-query";
import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  awsConnectionAuthorizationSchema,
  awsConnectionEnvelopeSchema,
  awsConnectionSchema,
  computeSummarySchema,
  connectionMachineListSchema,
  machineJoinCommandResponseSchema,
  machineSchema,
  unitMachineListSchema,
  type AwsConnection,
} from "@/lib/api/schemas";
import { accountQueryKeys, workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/**
 * The account's capacity announces every transition it makes, but only to the
 * workspaces a machine serves or is anchored in; somebody looking from another
 * workspace of the account would watch a node come up and never be told. Slow
 * because it is a backstop for that case rather than the mechanism.
 */
const ACCOUNT_CAPACITY_POLL_INTERVAL_MS = 30_000;

/**
 * Machines this account joined itself, each serving the workspaces it names.
 *
 * Every lifecycle write publishes `compute.machines` on the change stream of
 * each workspace the machine serves, which invalidates this list.
 */
export function machinesQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.compute.machines(),
    queryFn: () => apiRequest("/api/v1/machines/self-hosted?limit=250", unitMachineListSchema),
    refetchInterval: ACCOUNT_CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Machines the account's connected cloud launched, with their provider facts. */
export function connectionMachinesQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountQueryKeys.compute.instances(),
    enabled,
    queryFn: () => apiRequest("/api/v1/compute/instances", connectionMachineListSchema),
    refetchInterval: ACCOUNT_CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

/** The connected cloud's counts and cost, as the server classifies them. */
export function computeSummaryQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.compute.summary(workspaceId),
    queryFn: () =>
      apiRequest(withWorkspace("/api/v1/compute/summary", workspaceId), computeSummarySchema),
    refetchInterval: ACCOUNT_CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function awsConnectionQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountQueryKeys.compute.awsConnection(),
    enabled,
    queryFn: getAwsConnection,
    refetchInterval: ACCOUNT_CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

export async function getAwsConnection(): Promise<AwsConnection | null> {
  const response = await apiRequest("/api/v1/aws-connection", awsConnectionEnvelopeSchema);
  return response.connection;
}

export type CreateAwsConnectionInput = {
  accountId: string;
};

export function createAwsConnection(input: CreateAwsConnectionInput) {
  return postJson("/api/v1/aws-connection", awsConnectionAuthorizationSchema, {
    account_id: input.accountId,
  });
}

export function validateAwsConnection() {
  return postJson("/api/v1/aws-connection/validate", awsConnectionSchema);
}

export function reconnectAwsConnection() {
  return postJson("/api/v1/aws-connection/reconnect", awsConnectionAuthorizationSchema);
}

export async function removeAwsConnection(): Promise<AwsConnection | null> {
  const response = await apiRequest("/api/v1/aws-connection", awsConnectionEnvelopeSchema, {
    method: "DELETE",
  });
  return response.connection;
}

export function cancelAwsConnectionReconnect() {
  return apiRequest("/api/v1/aws-connection/reconnect", awsConnectionSchema, {
    method: "DELETE",
  });
}

export function retryAwsConnection() {
  return postJson("/api/v1/aws-connection/retry", awsConnectionSchema);
}

export type MachineJoinCommandInput = {
  name: string;
  workspaces: string[];
  gpu?: string[];
};

export function createMachineJoinCommand(input: MachineJoinCommandInput) {
  return postJson("/api/v1/machines/join-command", machineJoinCommandResponseSchema, {
    name: input.name,
    workspaces: input.workspaces,
    gpu: input.gpu ?? [],
  });
}

export function updateMachineWorkspaces(machineId: string, workspaces: string[]) {
  return apiRequest(`/api/v1/machines/${encodeURIComponent(machineId)}`, machineSchema, {
    method: "PATCH",
    body: JSON.stringify({ workspaces }),
  });
}

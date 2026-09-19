import { queryOptions } from "@tanstack/react-query";
import { apiRequest, postJson } from "@/lib/api/client";
import {
  awsConnectionAuthorizationSchema,
  awsConnectionEnvelopeSchema,
  awsConnectionSchema,
  connectionMachineListSchema,
  machineJoinCommandResponseSchema,
  machineSchema,
  unitMachineListSchema,
  type AwsConnection,
} from "@/lib/api/schemas";
import { accountQueryKeys, workspaceLiveQueryMeta } from "./workspace-keys";

/**
 * The connected cloud announces every transition it makes, but only to the
 * workspaces the account owns; somebody working inside a workspace they were
 * invited to would watch a stack finish and never be told. Slow because it is
 * a backstop for that case rather than the mechanism.
 */
const AWS_CONNECTION_POLL_INTERVAL_MS = 30_000;

/**
 * Machines this account joined itself, each serving the workspaces it names.
 *
 * Every lifecycle write publishes `compute.machines` on the change stream, so
 * the list is invalidated by the event rather than polled.
 */
export function machinesQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.compute.machines(),
    queryFn: () => apiRequest("/api/v1/machines/self-hosted?limit=250", unitMachineListSchema),
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Machines the account's connected cloud launched, with their provider facts. */
export function connectionMachinesQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountQueryKeys.compute.instances(),
    enabled,
    queryFn: () => apiRequest("/api/v1/compute/instances", connectionMachineListSchema),
    meta: workspaceLiveQueryMeta(true),
  });
}

export function awsConnectionQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountQueryKeys.compute.awsConnection(),
    enabled,
    queryFn: getAwsConnection,
    refetchInterval: AWS_CONNECTION_POLL_INTERVAL_MS,
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

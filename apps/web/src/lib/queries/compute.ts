import { queryOptions } from "@tanstack/react-query";
import { apiRequest, postJson } from "@/lib/api/client";
import {
  awsConnectionAuthorizationSchema,
  awsConnectionEnvelopeSchema,
  awsConnectionSchema,
  customerComputeCatalogSchema,
  customerComputeInstanceListSchema,
  poolJoinCommandResponseSchema,
  unitMachineListSchema,
  type AwsComputeConfigurationUpdateRequest,
  type AwsConnection,
} from "@/lib/api/schemas";
import { accountQueryKeys, workspaceLiveQueryMeta } from "./workspace-keys";

export const accountComputeQueryKeys = accountQueryKeys.compute;

/**
 * How often capacity is re-read while its panel is open.
 *
 * Readiness here is not a stored column the change stream can announce: the
 * server derives a machine's phase and an instance's service state from how
 * recently its agent was heard from. A host that dies stops sending, and
 * silence publishes nothing, so the only way the panel can show it leaving is
 * to ask again. These panels live inside the settings dialog, so the interval
 * runs while somebody is watching and stops with the tab.
 */
const CAPACITY_POLL_INTERVAL_MS = 5_000;

/**
 * The connected cloud announces every transition it makes, but only to the
 * workspaces the account owns; somebody working inside a workspace they were
 * invited to would watch a stack finish and never be told. Slow because it is
 * a backstop for that case rather than the mechanism.
 */
const AWS_CONNECTION_POLL_INTERVAL_MS = 30_000;

/** Machines this account connected. They serve every workspace it owns. */
export function machinesQueryOptions() {
  return queryOptions({
    queryKey: accountComputeQueryKeys.machines(),
    queryFn: () => apiRequest("/api/v1/machines/self-hosted?limit=250", unitMachineListSchema),
    refetchInterval: CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function computeInstancesQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountComputeQueryKeys.instances(),
    enabled,
    queryFn: () => apiRequest("/api/v1/compute/instances", customerComputeInstanceListSchema),
    refetchInterval: CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function computeCatalogQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountComputeQueryKeys.catalog(),
    enabled,
    queryFn: () => apiRequest("/api/v1/compute/catalog", customerComputeCatalogSchema),
    staleTime: 5 * 60_000,
  });
}

export function awsConnectionQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountComputeQueryKeys.awsConnection(),
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

/** Provisioning limits and defaults belong to the account, not to one workspace. */
export function updateAwsComputeConfiguration(
  request: AwsComputeConfigurationUpdateRequest,
): Promise<AwsConnection> {
  return apiRequest("/api/v1/aws-connection/compute", awsConnectionSchema, {
    method: "PUT",
    body: JSON.stringify(request),
  });
}

export type CreateAwsConnectionInput = {
  pool?: string;
  accountId: string;
  maxCpuInstances: number | null;
  maxGpuInstances: number | null;
};

export type AwsConnectionAuthorizationResult = {
  connection: AwsConnection;
  authorization: import("zod").z.infer<typeof awsConnectionAuthorizationSchema>["authorization"];
};

export async function createAwsConnection(
  input: CreateAwsConnectionInput,
): Promise<AwsConnectionAuthorizationResult> {
  const response = await postJson("/api/v1/aws-connection", awsConnectionAuthorizationSchema, {
    account_id: input.accountId,
    pool: input.pool,
    max_cpu_instances: input.maxCpuInstances,
    max_gpu_instances: input.maxGpuInstances,
  });
  return {
    connection: response.connection,
    authorization: response.authorization,
  };
}

export function validateAwsConnection() {
  return postJson("/api/v1/aws-connection/validate", awsConnectionSchema);
}

export async function reconnectAwsConnection(): Promise<AwsConnectionAuthorizationResult> {
  const response = await postJson(
    "/api/v1/aws-connection/reconnect",
    awsConnectionAuthorizationSchema,
  );
  return {
    connection: response.connection,
    authorization: response.authorization,
  };
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

export function createMachineJoinCommand() {
  return postJson("/api/v1/machines/join-command", poolJoinCommandResponseSchema);
}

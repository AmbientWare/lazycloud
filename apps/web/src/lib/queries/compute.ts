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
const AWS_CONNECTION_POLL_INTERVAL_MS = 30_000;

/** Machines this account connected. They serve every workspace it owns. */
export function machinesQueryOptions() {
  return queryOptions({
    queryKey: accountComputeQueryKeys.machines(),
    queryFn: () => apiRequest("/api/v1/machines/self-hosted?limit=250", unitMachineListSchema),
    refetchInterval: 5_000,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function computeInstancesQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountComputeQueryKeys.instances(),
    enabled,
    queryFn: () => apiRequest("/api/v1/compute/instances", customerComputeInstanceListSchema),
    refetchInterval: 5_000,
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
  accountId: string;
};

export type AwsConnectionAuthorizationResult = {
  connection: AwsConnection;
  authorization: { url: string | null };
};

export async function createAwsConnection(
  input: CreateAwsConnectionInput,
): Promise<AwsConnectionAuthorizationResult> {
  const response = await postJson("/api/v1/aws-connection", awsConnectionAuthorizationSchema, {
    account_id: input.accountId,
  });
  return {
    connection: response.connection,
    authorization: { url: response.authorization.url },
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
    authorization: { url: response.authorization.url },
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

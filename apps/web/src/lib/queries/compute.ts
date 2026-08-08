import { queryOptions } from "@tanstack/react-query";
import { ApiError, apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  awsConnectionAuthorizationSchema,
  awsConnectionEnvelopeSchema,
  awsConnectionSchema,
  customerComputeCatalogSchema,
  customerComputeInstanceListSchema,
  machinePoolListSchema,
  poolJoinCommandResponseSchema,
  unitMachineListSchema,
  workspaceComputePolicySchema,
  workerListSchema,
  type AwsConnection,
  type WorkspaceComputePolicy,
  type WorkspaceComputePolicyUpdateRequest,
} from "@/lib/api/schemas";
import { accountQueryKeys, workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export const computeQueryKeys = workspaceQueryKeys.compute;
export const accountComputeQueryKeys = accountQueryKeys.compute;
export const AWS_CONNECTION_POLL_INTERVAL_MS = 30_000;

/**
 * Capability probe for operator-only actions. A 403 is an authorization
 * decision and must not invalidate the user's otherwise valid session.
 */
export function adminAccessQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: computeQueryKeys.adminAccess(workspaceId),
    queryFn: async () => {
      try {
        await apiRequest(withWorkspace("/api/v1/workers", workspaceId), workerListSchema);
        return true;
      } catch (error) {
        if (error instanceof ApiError && error.status === 403) return false;
        throw error;
      }
    },
    staleTime: 5 * 60_000,
    retry: false,
  });
}

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

export function machinePoolsQueryOptions(workspaceId: string, enabled = true) {
  return queryOptions({
    queryKey: [...computeQueryKeys.root(workspaceId), "pools"] as const,
    enabled,
    queryFn: () =>
      apiRequest(withWorkspace("/api/v1/compute/pools", workspaceId), machinePoolListSchema),
  });
}

export function computePolicyQueryOptions(workspaceId: string, enabled = true) {
  return queryOptions({
    queryKey: computeQueryKeys.policy(workspaceId),
    enabled,
    queryFn: () => getComputePolicy(workspaceId),
  });
}

export function getComputePolicy(workspaceId: string): Promise<WorkspaceComputePolicy> {
  return apiRequest(
    withWorkspace("/api/v1/compute/policy", workspaceId),
    workspaceComputePolicySchema,
  );
}

export function updateComputePolicy(
  workspaceId: string,
  request: WorkspaceComputePolicyUpdateRequest,
): Promise<WorkspaceComputePolicy> {
  return apiRequest(
    withWorkspace("/api/v1/compute/policy", workspaceId),
    workspaceComputePolicySchema,
    {
      method: "PUT",
      body: JSON.stringify(request),
    },
  );
}

export function awsConnectionQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountComputeQueryKeys.awsConnection(),
    enabled,
    queryFn: async () => {
      const response = await apiRequest("/api/v1/aws-connection", awsConnectionEnvelopeSchema);
      return response.connection;
    },
    refetchInterval: AWS_CONNECTION_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
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

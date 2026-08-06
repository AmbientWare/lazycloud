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
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export const computeQueryKeys = workspaceQueryKeys.compute;
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

export function machinesQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: computeQueryKeys.machines(workspaceId),
    queryFn: () =>
      apiRequest(
        withWorkspace("/api/v1/machines/pool?pool=self-hosted&limit=250", workspaceId),
        unitMachineListSchema,
      ),
    refetchInterval: 5_000,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function computeInstancesQueryOptions(workspaceId: string, enabled = true) {
  return queryOptions({
    queryKey: computeQueryKeys.instances(workspaceId),
    enabled,
    queryFn: () =>
      apiRequest(
        withWorkspace("/api/v1/compute/instances", workspaceId),
        customerComputeInstanceListSchema,
      ),
    refetchInterval: 5_000,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function computeCatalogQueryOptions(workspaceId: string, enabled = true) {
  return queryOptions({
    queryKey: computeQueryKeys.catalog(workspaceId),
    enabled,
    queryFn: () =>
      apiRequest(
        withWorkspace("/api/v1/compute/catalog", workspaceId),
        customerComputeCatalogSchema,
      ),
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

export function awsConnectionQueryOptions(workspaceId: string, enabled = true) {
  return queryOptions({
    queryKey: computeQueryKeys.awsConnection(workspaceId),
    enabled,
    queryFn: async () => {
      const response = await apiRequest(
        withWorkspace("/api/v1/aws-connection", workspaceId),
        awsConnectionEnvelopeSchema,
      );
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
  workspaceId: string,
  input: CreateAwsConnectionInput,
): Promise<AwsConnectionAuthorizationResult> {
  const response = await postJson(
    withWorkspace("/api/v1/aws-connection", workspaceId),
    awsConnectionAuthorizationSchema,
    {
      account_id: input.accountId,
    },
  );
  return {
    connection: response.connection,
    authorization: { url: response.authorization.url },
  };
}

export function validateAwsConnection(workspaceId: string) {
  return postJson(
    withWorkspace("/api/v1/aws-connection/validate", workspaceId),
    awsConnectionSchema,
  );
}

export async function reconnectAwsConnection(
  workspaceId: string,
): Promise<AwsConnectionAuthorizationResult> {
  const response = await postJson(
    withWorkspace("/api/v1/aws-connection/reconnect", workspaceId),
    awsConnectionAuthorizationSchema,
  );
  return {
    connection: response.connection,
    authorization: { url: response.authorization.url },
  };
}

export async function removeAwsConnection(workspaceId: string): Promise<AwsConnection | null> {
  const response = await apiRequest(
    withWorkspace("/api/v1/aws-connection", workspaceId),
    awsConnectionEnvelopeSchema,
    { method: "DELETE" },
  );
  return response.connection;
}

export function cancelAwsConnectionReconnect(workspaceId: string) {
  return apiRequest(
    withWorkspace("/api/v1/aws-connection/reconnect", workspaceId),
    awsConnectionSchema,
    { method: "DELETE" },
  );
}

export function retryAwsConnection(workspaceId: string) {
  return postJson(withWorkspace("/api/v1/aws-connection/retry", workspaceId), awsConnectionSchema);
}

export function createMachineJoinCommand(workspaceId: string) {
  return postJson(
    withWorkspace("/api/v1/machines/join-command", workspaceId),
    poolJoinCommandResponseSchema,
  );
}

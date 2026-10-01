import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type {
  AwsAuthorizationGeneration,
  AwsConnection,
  AwsConnectionAuthorization,
  ComputeSummary,
  ConnectionMachine,
  UnitMachine,
} from "@/lib/api/schemas";
import { workspaceName } from "@/lib/api/workspaces";

import { accountQueryKeys, workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/**
 * The change stream carries no compute topics, so these reads poll. The
 * account's capacity and connection change slowly; machines poll faster
 * because the join dialog follows a host's progress through this list.
 */
const ACCOUNT_CAPACITY_POLL_INTERVAL_MS = 30_000;
const MACHINES_POLL_INTERVAL_MS = 5_000;
/** While the server moves the connection between phases without the customer. */
const CONNECTION_TRANSITION_POLL_INTERVAL_MS = 5_000;

/** Bounded single pages, as the settings lists read them. */
const MACHINE_PAGE_LIMIT = 250;
const INSTANCE_PAGE_LIMIT = 100;

type ListPage<TItem> = { data: TItem[]; next: string };

/** Machines this account joined itself, each serving the workspaces it names. */
export function machinesQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.compute.machines(),
    queryFn: async (): Promise<ListPage<UnitMachine>> => {
      const page = await ok(
        api.GET("/v1/machines", { params: { query: { limit: MACHINE_PAGE_LIMIT } } }),
      );
      return { data: page.machines.map(viewMachine), next: page.next_cursor ?? "" };
    },
    refetchInterval: MACHINES_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Machines the account's connected cloud launched, with their provider facts. */
export function connectionMachinesQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountQueryKeys.compute.instances(),
    enabled,
    queryFn: async (): Promise<ListPage<ConnectionMachine>> => {
      const page = await ok(
        api.GET("/v1/compute/instances", { params: { query: { limit: INSTANCE_PAGE_LIMIT } } }),
      );
      return { data: page.instances.map(viewInstance), next: page.next_cursor ?? "" };
    },
    refetchInterval: ACCOUNT_CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

/** The connected cloud's counts and cost, as the server classifies them. */
export function computeSummaryQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.compute.summary(workspaceId),
    queryFn: async (): Promise<ComputeSummary> => {
      const summary = await ok(
        api.GET("/v1/workspaces/{workspace}/compute", {
          params: { path: { workspace: workspaceName(workspaceId) } },
        }),
      );
      return {
        connection: summary.connection ?? null,
        instances: summary.instances,
        cost: {
          hourly_micros: summary.cost.hourly_micros ?? null,
          daily_micros: summary.cost.daily_micros ?? null,
          currency: summary.cost.currency,
          estimated: summary.cost.estimated,
        },
        workload_count: summary.workload_count,
      };
    },
    refetchInterval: ACCOUNT_CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function awsConnectionQueryOptions(enabled = true) {
  return queryOptions({
    queryKey: accountQueryKeys.compute.awsConnection(),
    enabled,
    queryFn: getAwsConnection,
    refetchInterval: (query) =>
      query.state.data && connectionIsTransitioning(query.state.data)
        ? CONNECTION_TRANSITION_POLL_INTERVAL_MS
        : ACCOUNT_CAPACITY_POLL_INTERVAL_MS,
    meta: workspaceLiveQueryMeta(true),
  });
}

function connectionIsTransitioning(connection: AwsConnection): boolean {
  switch (connection.phase) {
    case "validating":
    case "retiring_authorization":
    case "disconnect_draining":
    case "revoking":
    case "verifying_revocation":
      return true;
    case "awaiting_authorization":
    case "ready":
    case "degraded":
    case "reconnect_pending":
    case "action_required":
      return false;
  }
}

export async function getAwsConnection(): Promise<AwsConnection | null> {
  const response = await ok(api.GET("/v1/aws-connection"));
  return response.connection ? viewConnection(response.connection) : null;
}

export type CreateAwsConnectionInput = {
  accountId: string;
};

export async function createAwsConnection(
  input: CreateAwsConnectionInput,
): Promise<AwsConnectionAuthorization> {
  return viewAuthorization(
    await ok(api.POST("/v1/aws-connection", { body: { account_id: input.accountId } })),
  );
}

export async function validateAwsConnection(): Promise<AwsConnection> {
  return viewConnection(await ok(api.POST("/v1/aws-connection/validate")));
}

export async function reconnectAwsConnection(): Promise<AwsConnectionAuthorization> {
  return viewAuthorization(await ok(api.POST("/v1/aws-connection/reconnect", { body: {} })));
}

/** The connection while its removal runs, or null once it is gone. */
export async function removeAwsConnection(): Promise<AwsConnection | null> {
  const response = await ok(api.DELETE("/v1/aws-connection"));
  return response.connection ? viewConnection(response.connection) : null;
}

export async function cancelAwsConnectionReconnect(): Promise<AwsConnection> {
  return viewConnection(await ok(api.DELETE("/v1/aws-connection/reconnect")));
}

export async function retryAwsConnection(): Promise<AwsConnection> {
  return viewConnection(await ok(api.POST("/v1/aws-connection/retry")));
}

export type MachineJoinCommandInput = {
  name: string;
  workspaces: string[];
  gpu?: string[];
};

export async function createMachineJoinCommand(
  input: MachineJoinCommandInput,
): Promise<{ command: string; expires_at: string }> {
  const joined = await ok(
    api.POST("/v1/machines/join-command", {
      body: { name: input.name, workspaces: input.workspaces, gpu: input.gpu ?? [] },
    }),
  );
  return { command: joined.command, expires_at: joined.expires_at };
}

/** `machineId` may be the machine's id or its name. */
export async function updateMachineWorkspaces(
  machineId: string,
  workspaces: string[],
): Promise<UnitMachine> {
  return viewMachine(
    await ok(
      api.PATCH("/v1/machines/{machine}", {
        params: { path: { machine: machineId } },
        body: { workspaces },
      }),
    ),
  );
}

// The compute API's shapes as the settings views read them: absent optional
// fields become the nulls the views render as absence.

function viewMachine(machine: Schemas["Machine"]): UnitMachine {
  return {
    id: machine.id,
    name: machine.name,
    workspaces: machine.workspaces,
    cpu: machine.cpu,
    memory: machine.memory,
    gpu: machine.gpu,
    gpu_count: machine.gpu_count,
    placement: machine.placement,
    lifecycle: machine.lifecycle,
    lifecycle_message: machine.lifecycle_message,
    lifecycle_failure: machine.lifecycle_failure ?? null,
    lifecycle_at: machine.lifecycle_at,
    connected: machine.connected,
    schedulable: machine.schedulable,
    capacity_state: machine.capacity_state,
    capacity_reason: machine.capacity_reason,
    // The API reports the capacity state without when it was observed or announced.
    capacity_observed_at: null,
    capacity_notice_at: null,
    preflight_checks: machine.preflight_checks,
    remediation: machine.remediation,
    last_seen_at: machine.last_seen_at ?? null,
  };
}

function viewInstance(instance: Schemas["ComputeInstance"]): ConnectionMachine {
  return {
    id: instance.id,
    placement: instance.placement,
    provider: instance.provider,
    region: instance.region,
    availability_zone: instance.availability_zone,
    instance_id: instance.instance_id,
    instance_type: instance.instance_type,
    lifecycle: instance.lifecycle,
    lifecycle_message: instance.lifecycle_message,
    lifecycle_failure: instance.lifecycle_failure ?? null,
    lifecycle_at: instance.lifecycle_at,
    connected: instance.connected,
    capacity_state: instance.capacity_state,
    capacity_reason: instance.capacity_reason,
    gpu: instance.gpu || null,
    gpu_count: instance.gpu_count,
    cpu_millicores: instance.cpu_millicores,
    memory_mb: instance.memory_mb,
    launch_attempt: instance.launch_attempt,
    booted_template_version: instance.booted_template_version,
    launched_at: instance.launched_at ?? null,
    created_at: instance.created_at,
  };
}

function viewConnection(connection: Schemas["AwsConnection"]): AwsConnection {
  const action = connection.customer_action;
  return {
    id: connection.id,
    account_id: connection.account_id,
    phase: connection.phase,
    active_authorization: viewGeneration(connection.active_authorization),
    pending_authorization: viewGeneration(connection.pending_authorization),
    retiring_authorization: viewGeneration(connection.retiring_authorization),
    revision: connection.revision,
    hosts_workloads: connection.hosts_workloads,
    can_manage_existing_capacity: connection.can_manage_existing_capacity,
    available_actions: connection.available_actions,
    detail: connection.detail,
    customer_action: action
      ? { url: action.url ?? null, stack: action.stack ?? null, label: action.label }
      : null,
    next_retry_at: connection.next_retry_at ?? null,
    created_at: connection.created_at,
    updated_at: connection.updated_at,
  };
}

function viewGeneration(
  generation: Schemas["AwsAuthorizationGeneration"] | undefined,
): AwsAuthorizationGeneration | null {
  if (!generation) return null;
  const managed = generation.managed_authorization;
  return {
    generation: generation.generation,
    authorization_mode: generation.authorization_mode,
    managed_authorization: managed ? { ...managed, stack_id: managed.stack_id ?? null } : null,
    phase: generation.phase,
    last_validation_started_at: generation.last_validation_started_at ?? null,
    last_validated_at: generation.last_validated_at ?? null,
    error_code: generation.error_code ?? null,
    error_message: generation.error_message ?? null,
    created_at: generation.created_at,
    updated_at: generation.updated_at,
  };
}

function viewAuthorization(
  authorization: Schemas["AwsConnectionAuthorization"],
): AwsConnectionAuthorization {
  return {
    connection: viewConnection(authorization.connection),
    authorization: {
      stack: authorization.authorization.stack ?? null,
      external_id: authorization.authorization.external_id ?? null,
    },
  };
}

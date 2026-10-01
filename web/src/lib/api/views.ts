import type { Schemas } from "@/lib/api/client";
import type {
  App,
  Container,
  Deployment,
  FunctionResult,
  Stub,
  Task,
  TaskSummary,
  User,
  Workspace,
} from "@/lib/api/schemas";

/*
 * The public API's resources as the dashboard's components read them. The
 * components keep the reference's view types; this is where the new API's
 * shapes become those. A field the API does not provide gets the empty value
 * the components already render for absence.
 */

export function viewUser(user: Schemas["User"]): User {
  return {
    id: user.id,
    // The reference server normalized this to the GitHub name or login.
    display_name: user.display_name || user.github_login || user.email,
    email: user.email,
    avatar_url: user.avatar_url,
    github_user_id: "",
    github_login: user.github_login,
    role: user.is_admin ? "administrator" : "member",
    status: user.status,
    created_at: user.created_at,
    updated_at: user.created_at,
  };
}

export function viewWorkspace(workspace: Schemas["Workspace"]): Workspace {
  return {
    id: workspace.id,
    name: workspace.name,
    status: workspace.state,
    signing_key_prefix: null,
    primary_token_id: null,
    concurrency_limit_id: null,
    connection_id: null,
    storage: { backend: "s3", bucket: null, prefix: "" },
    labels: {},
    metadata: {},
    created_at: workspace.created_at,
    updated_at: workspace.created_at,
  };
}

export function viewApp(app: Schemas["App"], workspaceId: string): App {
  return {
    id: app.id,
    workspace_id: workspaceId,
    stub_id: null,
    name: app.name,
    version: 1,
    public: false,
    active: app.state === "active",
    deleted_at: null,
    created_at: app.created_at,
    updated_at: app.created_at,
    actions: {
      can_pause: app.state === "active",
      can_resume: app.state === "paused",
      can_delete: app.state !== "deleted",
    },
  };
}

/**
 * A workload's release as the reference's stub: opaque to components, it
 * names the app, function and release so the query layer can filter by it.
 */
export function stubId(app: string, name: string, releaseId: string): string {
  return `${app}:${name}:${releaseId}`;
}

export function parseStubId(id: string): { app: string; name: string; releaseId: string } {
  const [app = "", name = "", releaseId = ""] = id.split(":");
  return { app, name, releaseId };
}

/** One deployed version of a workload, as the reference's deployment row. */
export function deploymentId(workloadId: string, version: number): string {
  return `${workloadId}:${version}`;
}

export function parseDeploymentId(id: string): { workload: string; version: number | null } {
  const [workload = "", version] = id.split(":");
  return { workload, version: version ? Number(version) : null };
}

const NO_SPEC: Deployment["spec"] = {
  resources: {
    gpu: [],
    gpu_count: 0,
    concurrency: 1,
    availability_zone: "",
    preemptible: false,
  },
  methods: [],
  command: [],
  ports: {},
  machine: "",
};

/** A function's definition in the reference's deployment spec shape. */
export function viewSpec(spec: Schemas["FunctionSpec"] | undefined): Deployment["spec"] {
  if (!spec) return NO_SPEC;
  const { resources } = spec;
  const cpu = resources.cpu_millis / 1000;
  const memory = resources.memory_mib;
  return {
    resources: {
      cpu: resources.cpu_limit_millis ? [cpu, resources.cpu_limit_millis / 1000] : cpu,
      memory: resources.memory_limit_mib ? [memory, resources.memory_limit_mib] : memory,
      gpu: [],
      gpu_count: 0,
      timeout_seconds: spec.timeout_seconds ?? 3600,
      concurrency: spec.concurrency ?? 1,
      keep_warm: spec.keep_warm_seconds ?? null,
      availability_zone: "",
      preemptible: false,
    },
    route: spec.http?.route ?? null,
    methods: spec.http?.methods ?? [],
    cron: spec.cron ?? null,
    command: [],
    ports: {},
    machine: "",
  };
}

/**
 * One version of a deployed workload. Stop and start act on the workload, and
 * starting an inactive version makes it the active one. Deleting removes the
 * whole workload, so a row offers it only where it stands for the workload:
 * the workload's active row, or its last version.
 */
export function viewDeployment(
  workload: Schemas["DeployedWorkload"],
  appId: string,
  version: { version: number; release_id: string; active: boolean; created_at: string },
  options: { spec?: Schemas["FunctionSpec"]; deletesWorkload?: boolean } = {},
): Deployment {
  const running = version.active && workload.state === "active" && workload.app_state !== "paused";
  return {
    id: deploymentId(workload.id, version.version),
    name: workload.name,
    kind: workload.kind,
    role: null,
    app_id: appId,
    stub_id: stubId(workload.app, workload.name, version.release_id),
    version: version.version,
    spec: version.active ? viewSpec(options.spec) : NO_SPEC,
    active: running,
    deleted_at: null,
    created_at: version.created_at,
    updated_at: version.created_at,
    actions: {
      can_start: !running,
      can_stop: running,
      can_delete: Boolean(options.deletesWorkload) && workload.state !== "deleted",
      can_scale: false,
    },
    scaling: null,
  };
}

/** The active version of a workload as its deployment row, standing for the workload. */
export function viewActiveDeployment(
  workload: Schemas["DeployedWorkload"],
  appId: string,
  spec?: Schemas["FunctionSpec"],
): Deployment {
  return viewDeployment(
    workload,
    appId,
    {
      version: workload.version ?? 1,
      release_id: workload.release_id ?? "",
      active: true,
      created_at: workload.deployed_at ?? workload.created_at,
    },
    { spec, deletesWorkload: true },
  );
}

export function viewStub(
  workload: Schemas["DeployedWorkload"],
  workspaceId: string,
  appId: string,
  spec?: Schemas["FunctionSpec"],
): Stub {
  return {
    id: stubId(workload.app, workload.name, workload.release_id ?? ""),
    workspace_id: workspaceId,
    name: workload.name,
    kind: workload.kind,
    handler: spec?.handler ?? null,
    deployment_id: deploymentId(workload.id, workload.version ?? 1),
    app_id: appId,
    // Deployed with authorized=False, the workload answers its URLs without a token.
    public: spec?.authorized === false,
    config: { runtime: null },
    created_at: workload.created_at,
    updated_at: workload.deployed_at ?? workload.created_at,
  };
}

/** The reference's task status vocabulary. */
export function viewTaskStatus(task: Schemas["Task"]): string {
  switch (task.status) {
    case "queued":
      return task.attempts > 0 && task.next_attempt_at ? "retry" : "pending";
    case "running":
      return "running";
    case "succeeded":
      return "complete";
    case "failed":
      return task.failure?.kind === "timeout" ? "timeout" : "failed";
    case "cancelled":
      return "cancelled";
  }
}

/**
 * A status without the attempt detail `viewTaskStatus` reads: queued work is
 * pending and a failure is failed, whether or not it retries or timed out.
 */
export function viewStatus(status: Schemas["TaskStatus"]): string {
  switch (status) {
    case "queued":
      return "pending";
    case "succeeded":
      return "complete";
    case "running":
    case "failed":
    case "cancelled":
      return status;
  }
}

/** Task counts by status, keyed by the reference's statuses. */
export function viewStatusCounts(counts: Schemas["TaskStatusCounts"]): Record<string, number> {
  return {
    pending: counts.queued,
    running: counts.running,
    complete: counts.succeeded,
    failed: counts.failed,
    cancelled: counts.cancelled,
  };
}

/** The API status a reference status filter asks for. */
export function apiTaskStatus(status: string | undefined): Schemas["TaskStatus"] | undefined {
  switch (status) {
    case "pending":
    case "retry":
      return "queued";
    case "running":
      return "running";
    case "complete":
      return "succeeded";
    case "failed":
    case "timeout":
      return "failed";
    case "cancelled":
      return "cancelled";
    default:
      return undefined;
  }
}

export function viewTaskSummary(
  task: Schemas["Task"],
  workspaceId: string,
  appId: string | null,
  workloadId: string | null,
): TaskSummary {
  const status = viewTaskStatus(task);
  const terminal = ["complete", "failed", "timeout", "cancelled"].includes(status);
  return {
    id: task.id,
    name: task.function,
    status,
    pending_progress: task.status === "queued" ? (task.pending ?? null) : null,
    workspace_id: workspaceId,
    app_id: appId,
    stub_id: stubId(task.app, task.function, task.release_id),
    deployment_id: workloadId && task.version ? deploymentId(workloadId, task.version) : null,
    container_id: task.container_id ?? null,
    parent_task_id: task.parent_task_id ?? null,
    root_task_id: task.root_task_id,
    handler: null,
    attempt_number: task.attempts,
    max_attempts: task.max_attempts,
    next_retry_at: task.next_attempt_at ?? null,
    exit_code: null,
    created_at: task.created_at,
    started_at: task.started_at ?? null,
    finished_at: task.finished_at ?? null,
    app: { name: task.app },
    workload: { name: task.function, kind: "function" },
    deployment: task.version ? { name: task.function, version: task.version } : null,
    actions: { can_cancel: !terminal, can_rerun: terminal, can_shell: false },
  };
}

export function viewTask(
  task: Schemas["Task"],
  workspaceId: string,
  appId: string | null,
  workloadId: string | null,
  extras: { result: FunctionResult | null; container: Container | null },
): Task {
  return {
    ...viewTaskSummary(task, workspaceId, appId, workloadId),
    command: [],
    args: [],
    kwargs: {},
    result: extras.result,
    error: task.failure ? failureText(task.failure) : null,
    container: extras.container,
  };
}

/**
 * An endpoint or ASGI request as the task row the reference listed for it.
 * The edge records a request once it ends: failed when the workload answered
 * with a server error, cancelled when the caller left first (499), otherwise
 * complete.
 */
function requestStatus(status: number): string {
  if (status === 499) return "cancelled";
  return status >= 500 ? "failed" : "complete";
}

export function viewRequestSummary(
  request: Schemas["HttpRequest"],
  workspaceId: string,
  appId: string | null,
  workloadId: string | null,
): TaskSummary {
  const finished = new Date(Date.parse(request.started_at) + request.duration_ms).toISOString();
  return {
    id: request.id,
    name: request.name,
    status: requestStatus(request.status),
    pending_progress: null,
    workspace_id: workspaceId,
    app_id: appId,
    stub_id: stubId(request.app, request.name, request.release_id),
    deployment_id: workloadId && request.version ? deploymentId(workloadId, request.version) : null,
    container_id: request.container_id ?? null,
    parent_task_id: null,
    root_task_id: request.id,
    handler: null,
    attempt_number: 1,
    max_attempts: 1,
    next_retry_at: null,
    exit_code: null,
    created_at: request.started_at,
    started_at: request.started_at,
    finished_at: finished,
    app: { name: request.app },
    workload: { name: request.name, kind: request.kind },
    deployment: request.version ? { name: request.name, version: request.version } : null,
    actions: { can_cancel: false, can_rerun: false, can_shell: false },
  };
}

export function viewRequest(
  request: Schemas["HttpRequest"],
  workspaceId: string,
  appId: string | null,
  workloadId: string | null,
  container: Container | null,
): Task {
  return {
    ...viewRequestSummary(request, workspaceId, appId, workloadId),
    command: [request.method, request.path],
    args: [],
    kwargs: {},
    result: null,
    error:
      request.status >= 500 ? `${request.method} ${request.path} answered ${request.status}` : null,
    container,
  };
}

function failureText(failure: Schemas["TaskFailure"]): string {
  const heading = failure.type ? `${failure.type}: ${failure.message}` : failure.message;
  return failure.traceback ? `${failure.traceback.trimEnd()}` : heading;
}

/** A stored result in the reference's function result shape. */
export async function viewResult(payload: Schemas["Payload"]): Promise<FunctionResult> {
  if (payload.encoding === "json") {
    return { version: 1, encoding: "json", value: (payload.value ?? null) as never };
  }
  const data = payload.data ?? "";
  const bytes = Uint8Array.from(atob(data), (character) => character.charCodeAt(0));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return {
    version: 1,
    encoding: "cloudpickle",
    value_base64: data,
    size_bytes: bytes.byteLength,
    sha256: Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join(""),
    display: null,
  };
}

const STOP_REASONS: Partial<Record<Schemas["StopReason"], string>> = {
  out_of_memory: "MEMORY_EVICTED",
  host_lost: "PREEMPTED",
};

/** The reference's container status: pending, running, stopped or failed. */
function containerStatus(container: Schemas["Container"]): string {
  switch (container.state) {
    case "pending":
    case "starting":
      return "pending";
    case "ready":
    case "draining":
      return "running";
    case "stopped":
      return container.stop_reason === undefined || container.stop_reason === "stopped"
        ? "stopped"
        : "failed";
  }
}

export function viewContainer(
  container: Schemas["Container"],
  workspaceId: string,
  appId: string | null,
): Container {
  return {
    id: container.id,
    name: `${container.function}-${container.id.slice(0, 8)}`,
    image: "",
    workspace_id: workspaceId,
    stub_id: stubId(container.app, container.function, container.release_id),
    app_id: appId,
    machine_id: null,
    worker_id: null,
    runtime_machine_id: "",
    runtime_worker_id: "",
    task_id: null,
    status: containerStatus(container),
    exit_code: null,
    termination_reason: (container.stop_reason && STOP_REASONS[container.stop_reason]) ?? "UNKNOWN",
    command: [],
    cwd: null,
    ports: {},
    created_at: container.created_at,
    started_at: container.ready_at ?? null,
    finished_at: container.stopped_at ?? null,
  };
}

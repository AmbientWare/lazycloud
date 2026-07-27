import { e as env } from "./env-vgS3Y8Xp.mjs";
import { c as createServerFn, T as TSS_SERVER_FUNCTION, a as getServerFnById } from "./index.mjs";
import { c as createMiddleware } from "./createMiddleware-CRzJRBrm.mjs";
import { j as object, k as string, p as number, m as array, q as boolean, t as record, _ as _enum, w as any } from "../_libs/zod.mjs";
const createSsrRpc = (functionId, importer) => {
  const url = "/_serverFn/" + functionId;
  const serverFnMeta = { id: functionId };
  const fn = async (...args) => {
    const serverFn = await getServerFnById(functionId);
    return serverFn(...args);
  };
  return Object.assign(fn, {
    url,
    serverFnMeta,
    [TSS_SERVER_FUNCTION]: true
  });
};
const getSignOutUrl = createServerFn({
  method: "POST"
}).inputValidator((options) => options).handler(createSsrRpc("7be4f3f67f835c7721278639b4cf286335026dc9c8df2d003bd35bccb299b3aa"));
createServerFn({
  method: "POST"
}).inputValidator((options) => options).handler(createSsrRpc("2771d306e92d595151b6182fd5cf0871e36cdcfb9fe6b4342da1e3d463652a42"));
const getAuth = createServerFn({
  method: "GET"
}).handler(createSsrRpc("4ecbfcb5f42faeafc3cdb09e6a6439b914ef8dfe50140cdf7b4a3ec86bcbb687"));
createServerFn({
  method: "GET"
}).inputValidator((options) => options).handler(createSsrRpc("b798b2ce6f01778c45e438761fc4a97a0c74e9956f750ec647c187278a85a637"));
createServerFn({
  method: "GET"
}).inputValidator((data) => data).handler(createSsrRpc("cd661a1c0e05868ca5865a5775b777eac01f13ed0648161834275b0b4faaf479"));
createServerFn({
  method: "GET"
}).inputValidator((data) => data).handler(createSsrRpc("8a1b7363180c62efb62991a3faa87594c774ba4b5a9de9b5ec117d2ce007fddb"));
createServerFn({
  method: "POST"
}).inputValidator((data) => data).handler(createSsrRpc("5f24552b9f049332d1d320fde6fc92f2a0a67aa3c8e2d907d49c1d3207c1d457"));
const ResourceRequirementsSchema = object({
  cpu: string().nullable().optional(),
  memory: string().nullable().optional()
});
const ResourcesSchema = object({
  limits: ResourceRequirementsSchema.nullable().optional(),
  requests: ResourceRequirementsSchema.nullable().optional()
});
const CurrentUsageSchema = object({
  cpu: string().nullable().optional(),
  memory: string().nullable().optional()
});
const HttpGetProbeSchema = object({
  path: string(),
  port: number(),
  scheme: string().nullable().optional()
});
const TcpSocketProbeSchema = object({
  port: number()
});
const ExecProbeSchema = object({
  command: array(string())
});
const ProbeConfigSchema = object({
  httpGet: HttpGetProbeSchema.nullable().optional(),
  tcpSocket: TcpSocketProbeSchema.nullable().optional(),
  exec: ExecProbeSchema.nullable().optional(),
  initialDelaySeconds: number().nullable().optional(),
  timeoutSeconds: number().nullable().optional(),
  periodSeconds: number().nullable().optional(),
  successThreshold: number().nullable().optional(),
  failureThreshold: number().nullable().optional()
});
const HealthCheckValuesSchema = object({
  enabled: boolean().optional(),
  livenessProbe: ProbeConfigSchema.nullable().optional(),
  readinessProbe: ProbeConfigSchema.nullable().optional()
});
const HPAMetricSchema = object({
  type: string(),
  resource: record(string(), any())
});
const HPAValuesSchema = object({
  enabled: boolean().optional(),
  minReplicas: number().optional(),
  maxReplicas: number().optional(),
  metrics: array(HPAMetricSchema).optional()
});
const PodStatusSchema = object({
  name: string(),
  phase: string(),
  ready_containers: number(),
  total_containers: number(),
  restart_count: number(),
  age: string(),
  node: string(),
  ip: string().nullable().optional(),
  cpu_usage: string().nullable().optional(),
  memory_usage: string().nullable().optional(),
  reason: string().nullable().optional(),
  message: string().nullable().optional()
});
const DeploymentOverviewSchema = object({
  id: string(),
  workspace_id: string(),
  name: string(),
  namespace: string(),
  state: string(),
  status_message: string().nullable(),
  created_at: string().nullable(),
  updated_at: string().nullable(),
  deployed_at: string().nullable(),
  service_count: number(),
  volume_count: number(),
  network_count: number(),
  ready_services: number().nullable()
});
const ServiceStatusSummarySchema = object({
  name: string(),
  status: string(),
  ready_replicas: number(),
  total_replicas: number(),
  image: string().nullable(),
  ports: array(string()).nullable(),
  restarts: number(),
  endpoint: string().nullable(),
  // Custom domain fields
  custom_domain: string().nullable().optional(),
  domain_status: string().nullable().optional(),
  cname_target: string().nullable().optional(),
  // Detailed fields for service details view
  resources: ResourcesSchema.nullable().optional(),
  current_usage: CurrentUsageSchema.nullable().optional(),
  healthcheck: HealthCheckValuesSchema.nullable().optional(),
  hpa: HPAValuesSchema.nullable().optional(),
  pods: array(PodStatusSchema).nullable().optional()
});
const VolumeStatusSummarySchema = object({
  name: string(),
  status: string(),
  storage_type: string(),
  size: string().nullable().optional()
});
const DeploymentStatusSchema = object({
  deployment_id: string(),
  deployment_name: string(),
  namespace: string(),
  status: string(),
  ready: boolean(),
  last_checked: string(),
  deployed_at: string(),
  total_services: number(),
  ready_services: number(),
  total_replicas: number(),
  ready_replicas: number(),
  services: array(ServiceStatusSummarySchema),
  volumes: array(VolumeStatusSummarySchema).nullable(),
  networks: array(
    object({
      name: string(),
      status: string(),
      driver: string().nullable()
    })
  ).nullable()
});
object({
  status: DeploymentStatusSchema
});
const WorkspaceRoles = {
  OWNER: "owner",
  ADMIN: "admin",
  MEMBER: "member"
};
const WorkspaceRoleSchema = _enum([
  WorkspaceRoles.OWNER,
  WorkspaceRoles.ADMIN,
  WorkspaceRoles.MEMBER
]);
object({
  id: string(),
  name: string(),
  is_personal: boolean(),
  role: WorkspaceRoleSchema
});
const WorkspaceMemberSchema = object({
  user_id: string().nullable(),
  name: string().nullable(),
  email: string(),
  role: WorkspaceRoleSchema,
  status: string(),
  invitation_id: string().nullable().optional()
});
object({
  id: string(),
  name: string(),
  is_personal: boolean(),
  role: WorkspaceRoleSchema,
  has_more: boolean(),
  deployments: array(DeploymentOverviewSchema),
  cursor: string().nullable()
});
function canInviteMembers(role) {
  return role === WorkspaceRoles.OWNER || role === WorkspaceRoles.ADMIN;
}
function canManageMembers(role) {
  return role === WorkspaceRoles.OWNER || role === WorkspaceRoles.ADMIN;
}
class LazyCloudAPIClass {
  static adminApiKey = env.ADMIN_API_KEY || null;
  apiUrl = env.API_URL || "http://localhost:8000";
  static VERSION_PREFIX = env.API_PREFIX || "/v1";
  // Utility Methods
  async fetchWithApiUrl(endpoint, options, authOptions) {
    const headers = new Headers(options?.headers);
    if (options?.body) {
      headers.set("Content-Type", "application/json");
    }
    const useAdmin = authOptions?.useAdmin ?? false;
    const token = useAdmin ? LazyCloudAPIClass.adminApiKey : authOptions?.accessToken ?? null;
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    } else if (authOptions !== void 0 && (authOptions.accessToken !== void 0 || authOptions.useAdmin)) {
      throw new Error("No authentication token provided");
    }
    const response = await fetch(
      `${this.apiUrl}${LazyCloudAPIClass.VERSION_PREFIX}${endpoint}`,
      {
        ...options,
        headers,
        mode: "cors"
      }
    );
    if (!response.ok) {
      let errorMessage;
      try {
        const errorData = await response.json();
        errorMessage = errorData.detail ?? errorData.error ?? JSON.stringify(errorData);
      } catch {
        errorMessage = `HTTP ${response.status}: ${response.statusText}`;
      }
      throw new Error(errorMessage);
    }
    const responseData = await response.json();
    if (typeof responseData === "object" && responseData !== null && "message" in responseData) {
      return responseData.data;
    }
    return responseData;
  }
  async get(endpoint, authOptions) {
    return await this.fetchWithApiUrl(
      endpoint,
      {
        method: "GET"
      },
      authOptions
    );
  }
  async post(endpoint, data, authOptions) {
    return await this.fetchWithApiUrl(
      endpoint,
      {
        method: "POST",
        body: JSON.stringify(data)
      },
      authOptions
    );
  }
  async delete(endpoint, authOptions) {
    return await this.fetchWithApiUrl(
      endpoint,
      {
        method: "DELETE"
      },
      authOptions
    );
  }
  async put(endpoint, data, authOptions) {
    return await this.fetchWithApiUrl(
      endpoint,
      {
        method: "PUT",
        body: JSON.stringify(data)
      },
      authOptions
    );
  }
  async patch(endpoint, data, authOptions) {
    return await this.fetchWithApiUrl(
      endpoint,
      {
        method: "PATCH",
        body: JSON.stringify(data)
      },
      authOptions
    );
  }
  // onboarding methods
  async onboardUser(workosId, name, email) {
    const data = {
      workos_id: workosId,
      name,
      email
    };
    const res = await this.post(
      `/users/onboarding`,
      data,
      {
        useAdmin: true
      }
    );
    return res.success;
  }
  // Workspace Methods
  async getWorkspaces(accessToken, startDate, endDate) {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get(`/workspaces${queryString}`, {
      accessToken
    });
  }
  async getWorkspace(accessToken, workspaceId) {
    const workspaces = await this.getWorkspaces(accessToken);
    const workspace = workspaces.find((w) => w.id === workspaceId);
    if (!workspace) {
      throw new Error("Workspace not found");
    }
    return workspace;
  }
  async getWorkspaceWithDeployments(accessToken, workspaceId) {
    return await this.get(
      `/workspaces/${workspaceId}/with-deployments`,
      { accessToken }
    );
  }
  async createWorkspace(accessToken, name) {
    const data = {
      name
    };
    return await this.post(`/workspaces`, data, { accessToken });
  }
  async deleteWorkspace(accessToken, workspaceId) {
    return await this.delete(
      `/workspaces/${workspaceId}`,
      { accessToken }
    );
  }
  async leaveWorkspace(accessToken, workspaceId) {
    return await this.post(
      `/workspaces/${workspaceId}/members/leave`,
      {},
      { accessToken }
    );
  }
  async getCurrentUser(accessToken) {
    return await this.get(`/users/current`, {
      accessToken
    });
  }
  async getUserFeatures(accessToken) {
    return await this.get(`/users/features`, {
      accessToken
    });
  }
  async getWorkspaceMembers(accessToken, workspaceId) {
    const response = await this.get(
      `/workspaces/${workspaceId}/members`,
      { accessToken }
    );
    return response.map((member) => WorkspaceMemberSchema.parse(member));
  }
  async inviteUser(accessToken, workspaceId, email, role = "member", acceptanceUrl) {
    const data = {
      email,
      role,
      acceptance_url: acceptanceUrl
    };
    return await this.post(
      `/workspaces/${workspaceId}/members/invite`,
      data,
      { accessToken }
    );
  }
  async updateMemberRole(accessToken, workspaceId, memberUserId, role) {
    const data = {
      user_id: memberUserId,
      role
    };
    return await this.patch(
      `/workspaces/${workspaceId}/members/${memberUserId}/role`,
      data,
      { accessToken }
    );
  }
  async transferOwnership(accessToken, workspaceId, newOwnerUserId, acceptanceUrl) {
    const data = {
      new_owner_user_id: newOwnerUserId,
      acceptance_url: acceptanceUrl
    };
    return await this.post(
      `/workspaces/${workspaceId}/members/transfer-ownership`,
      data,
      { accessToken }
    );
  }
  async removeMember(accessToken, workspaceId, memberUserId) {
    return await this.delete(
      `/workspaces/${workspaceId}/members/${memberUserId}`,
      { accessToken }
    );
  }
  async cancelInvitation(accessToken, workspaceId, invitationId) {
    return await this.delete(
      `/workspaces/${workspaceId}/members/invitations/${invitationId}`,
      { accessToken }
    );
  }
  async getPendingOwnershipTransfer(accessToken, workspaceId) {
    return await this.get(
      `/workspaces/${workspaceId}/members/transfer-ownership/pending`,
      { accessToken }
    );
  }
  // Deployment Methods
  async getDeploymentStatus(accessToken, deploymentId) {
    return await this.get(
      `/deployments/${deploymentId}/status`,
      { accessToken }
    );
  }
  // Usage Methods
  async getAggregatedUsage(accessToken, startDate, endDate) {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get(
      `/workspaces/usage/all${queryString}`,
      { accessToken }
    );
  }
  async getAggregatedDailyUsage(accessToken, startDate, endDate, timezone) {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    if (timezone) params.append("timezone_str", timezone);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get(
      `/workspaces/usage/all/daily${queryString}`,
      { accessToken }
    );
  }
  async getDeploymentCostBreakdown(accessToken, deploymentId, startDate, endDate) {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get(
      `/deployments/${deploymentId}/usage/breakdown${queryString}`,
      { accessToken }
    );
  }
  async getMeterPricing() {
    return await this.get("/billing/meter-pricing");
  }
  async getBillingCycle(accessToken) {
    return await this.get("/billing/cycle", {
      accessToken
    });
  }
  async acceptInvitation(accessToken, invitationId) {
    return await this.post(
      `/invitations/${invitationId}/accept`,
      {},
      { accessToken }
    );
  }
  async getPendingInvitations(accessToken) {
    return await this.get(`/invitations/pending`, { accessToken });
  }
  async declineInvitation(accessToken, invitationId) {
    return await this.post(
      `/invitations/${invitationId}/decline`,
      {},
      { accessToken }
    );
  }
  // API Key Methods
  async getApiKeys(accessToken) {
    return await this.get(`/api-keys`, { accessToken });
  }
  async regenerateApiKey(accessToken, apiKeyId) {
    return await this.put(`/api-keys/${apiKeyId}`, {}, { accessToken });
  }
  // Feedback Methods
  async submitFeedback(accessToken, feedbackType, message, source = "web") {
    return await this.post(
      `/feedback`,
      {
        feedback_type: feedbackType,
        message,
        source
      },
      { accessToken }
    );
  }
}
const lazycloudApi = new LazyCloudAPIClass();
const authMiddleware = createMiddleware({ type: "function" }).server(
  async ({ next }) => {
    const auth = await getAuth();
    if (!auth.user) {
      throw new Error("Not authenticated");
    }
    return next({
      context: {
        accessToken: auth.accessToken
      }
    });
  }
);
const userMiddleware = createMiddleware({ type: "function" }).server(
  async ({ next }) => {
    const auth = await getAuth();
    if (!auth.user) {
      throw new Error("Not authenticated");
    }
    return next({
      context: {
        accessToken: auth.accessToken,
        userId: auth.user.id
      }
    });
  }
);
export {
  WorkspaceRoles as W,
  authMiddleware as a,
  getSignOutUrl as b,
  createSsrRpc as c,
  canManageMembers as d,
  canInviteMembers as e,
  getAuth as g,
  lazycloudApi as l,
  userMiddleware as u
};

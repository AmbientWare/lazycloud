import { env } from "@/env";
import type { ApiKey, ApiKeyExpiresAtOptions } from "@/interfaces/apiKeys";
import type {
  Workspace,
  WorkspaceMember,
  WorkspaceWithDeploymentsResponse,
} from "@/interfaces/workspaces";
import { WorkspaceMemberSchema } from "@/interfaces/workspaces";
import type { DeploymentStatusResponse } from "@/interfaces/deployments";
import type {
  AggregatedUsageResponse,
  AggregatedDailyUsageResponse,
  WorkspaceCostBreakdownResponse,
  MeterPricingResponse,
} from "@/interfaces/usage";
import type { UserFeaturesResponse } from "@/interfaces/users";
import { generateJwtToken } from "@/server/jwt";

class LazyCloudAPIClass {
  private static adminApiKey = env.ADMIN_API_KEY || null;
  private apiUrl: string = env.API_URL || "http://localhost:8000";
  private static VERSION_PREFIX = env.API_PREFIX || "/v1";

  // Utility Methods
  private async fetchWithApiUrl<T>(
    endpoint: string,
    options?: RequestInit,
    authOptions?: { useAdmin?: boolean; userId?: string },
  ): Promise<T> {
    const headers = new Headers(options?.headers);

    if (options?.body) {
      headers.set("Content-Type", "application/json");
    }

    // Determine which auth token to use
    const useAdmin = authOptions?.useAdmin ?? false;
    const token = useAdmin
      ? LazyCloudAPIClass.adminApiKey
      : authOptions?.userId
        ? generateJwtToken(authOptions.userId)
        : null;

    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    } else if (authOptions !== undefined && (authOptions.userId !== undefined || authOptions.useAdmin)) {
      // Only require auth if userId or useAdmin was explicitly provided
      throw new Error("No authentication token provided");
    }

    const response = await fetch(
      `${this.apiUrl}${LazyCloudAPIClass.VERSION_PREFIX}${endpoint}`,
      {
        ...options,
        headers,
        mode: "cors",
      },
    );

    if (!response.ok) {
      let errorMessage: string;
      try {
        const errorData = (await response.json()) as {
          detail?: string;
          error?: string;
        };
        errorMessage =
          errorData.detail ?? errorData.error ?? JSON.stringify(errorData);
      } catch {
        // If JSON parsing fails, use the status text
        errorMessage = `HTTP ${response.status}: ${response.statusText}`;
      }
      throw new Error(errorMessage);
    }

    const responseData = (await response.json()) as
      | { message?: boolean; data?: T }
      | T;
    if (
      typeof responseData === "object" &&
      responseData !== null &&
      "message" in responseData
    ) {
      return responseData.data as T;
    }

    return responseData as T;
  }

  private async get<T>(
    endpoint: string,
    authOptions?: { useAdmin?: boolean; userId?: string },
  ): Promise<T> {
    return await this.fetchWithApiUrl<T>(
      endpoint,
      {
        method: "GET",
      },
      authOptions,
    );
  }

  private async post<T>(
    endpoint: string,
    data: Record<string, unknown>,
    authOptions?: { useAdmin?: boolean; userId?: string },
  ): Promise<T> {
    return await this.fetchWithApiUrl<T>(
      endpoint,
      {
        method: "POST",
        body: JSON.stringify(data),
      },
      authOptions,
    );
  }

  private async put<T>(
    endpoint: string,
    data: Record<string, unknown>,
    authOptions?: { useAdmin?: boolean; userId?: string },
  ): Promise<T> {
    return await this.fetchWithApiUrl<T>(
      endpoint,
      {
        method: "PUT",
        body: JSON.stringify(data),
      },
      authOptions,
    );
  }

  private async delete<T>(
    endpoint: string,
    authOptions?: { useAdmin?: boolean; userId?: string },
  ): Promise<T> {
    return await this.fetchWithApiUrl<T>(
      endpoint,
      {
        method: "DELETE",
      },
      authOptions,
    );
  }

  // onboarding methods

  async onboardUser(
    workosId: string,
    name: string,
    email: string,
  ): Promise<boolean> {
    const data = {
      workos_id: workosId,
      name: name,
      email: email,
    };
    const res = await this.post<{ success: boolean }>(
      `/users/onboarding`,
      data,
      { useAdmin: true },
    );
    return res.success;
  }

  // Api Keys Methods

  async getApiKeys(userId: string): Promise<ApiKey[]> {
    return await this.get<ApiKey[]>(`/api-keys?user_id=${userId}`, { userId });
  }

  async createApiKey(
    userId: string,
    name: string,
    expiresAt: ApiKeyExpiresAtOptions = "30",
  ): Promise<ApiKey> {
    const data = {
      name: name,
      workos_id: userId,
      expires_at: expiresAt,
    };
    return await this.post<ApiKey>(`/api-keys`, data, { userId });
  }

  async updateApiKey(
    userId: string,
    apiKeyId: string,
    expiresAt: ApiKeyExpiresAtOptions = "30",
  ): Promise<ApiKey> {
    const data = {
      workos_id: userId,
      expires_at: expiresAt,
    };
    return await this.put<ApiKey>(`/api-keys/${apiKeyId}`, data, { userId });
  }

  async deleteApiKey(userId: string, apiKeyId: string): Promise<ApiKey> {
    const params = new URLSearchParams({
      workos_id: userId,
      api_key_id: apiKeyId,
    }).toString();

    const deletedKeys = await this.delete<ApiKey[]>(`/api-keys?${params}`, {
      userId,
    });
    const deletedKey = deletedKeys[0];
    if (!deletedKey) throw new Error("No API key was deleted");
    return deletedKey;
  }

  // Workspace Methods

  async getWorkspaces(
    userId: string,
    startDate?: string,
    endDate?: string,
  ): Promise<Workspace[]> {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get<Workspace[]>(`/workspaces${queryString}`, { userId });
  }

  async getWorkspace(userId: string, workspaceId: string): Promise<Workspace> {
    const workspaces = await this.getWorkspaces(userId);
    const workspace = workspaces.find((w) => w.id === workspaceId);
    if (!workspace) {
      throw new Error("Workspace not found");
    }
    return workspace;
  }

  async getWorkspaceWithDeployments(
    userId: string,
    workspaceId: string,
  ): Promise<WorkspaceWithDeploymentsResponse> {
    return await this.get<WorkspaceWithDeploymentsResponse>(
      `/workspaces/${workspaceId}/with-deployments`,
      { userId },
    );
  }

  async createWorkspace(userId: string, name: string): Promise<Workspace> {
    const data = {
      name: name,
    };
    return await this.post<Workspace>(`/workspaces`, data, { userId });
  }

  async deleteWorkspace(
    userId: string,
    workspaceId: string,
  ): Promise<{ success: boolean }> {
    return await this.delete<{ success: boolean }>(
      `/workspaces/${workspaceId}`,
      { userId },
    );
  }

  async leaveWorkspace(
    userId: string,
    workspaceId: string,
  ): Promise<{ success: boolean }> {
    return await this.post<{ success: boolean }>(
      `/workspaces/${workspaceId}/members/leave`,
      {},
      { userId },
    );
  }

  async getCurrentUser(userId: string): Promise<{ id: string }> {
    return await this.get<{ id: string }>(`/users/current`, { userId });
  }

  async getUserFeatures(userId: string): Promise<UserFeaturesResponse> {
    return await this.get<UserFeaturesResponse>(`/users/features`, { userId });
  }

  async getWorkspaceMembers(
    userId: string,
    workspaceId: string,
  ): Promise<WorkspaceMember[]> {
    const response = await this.get<WorkspaceMember[]>(
      `/workspaces/${workspaceId}/members`,
      { userId },
    );
    return response.map((member) => WorkspaceMemberSchema.parse(member));
  }

  async inviteUser(
    userId: string,
    workspaceId: string,
    email: string,
    role = "member",
    acceptanceUrl?: string,
  ): Promise<{ success: boolean; token?: string }> {
    const data = {
      email: email,
      role: role,
      acceptance_url: acceptanceUrl,
    };
    return await this.post<{ success: boolean; token?: string }>(
      `/workspaces/${workspaceId}/members/invite`,
      data,
      { userId },
    );
  }

  async updateMemberRole(
    userId: string,
    workspaceId: string,
    memberUserId: string,
    role: string,
  ): Promise<WorkspaceMember> {
    const data = {
      user_id: memberUserId,
      role: role,
    };
    return await this.patch<WorkspaceMember>(
      `/workspaces/${workspaceId}/members/${memberUserId}/role`,
      data,
      { userId },
    );
  }

  async transferOwnership(
    userId: string,
    workspaceId: string,
    newOwnerUserId: string,
    acceptanceUrl?: string,
  ): Promise<{ success: boolean }> {
    const data = {
      new_owner_user_id: newOwnerUserId,
      acceptance_url: acceptanceUrl,
    };
    return await this.post<{ success: boolean }>(
      `/workspaces/${workspaceId}/members/transfer-ownership`,
      data,
      { userId },
    );
  }

  async removeMember(
    userId: string,
    workspaceId: string,
    memberUserId: string,
  ): Promise<{ success: boolean }> {
    return await this.delete<{ success: boolean }>(
      `/workspaces/${workspaceId}/members/${memberUserId}`,
      { userId },
    );
  }

  async cancelInvitation(
    userId: string,
    workspaceId: string,
    invitationId: string,
  ): Promise<{ success: boolean }> {
    return await this.delete<{ success: boolean }>(
      `/workspaces/${workspaceId}/members/invitations/${invitationId}`,
      { userId },
    );
  }

  async getPendingOwnershipTransfer(
    userId: string,
    workspaceId: string,
  ): Promise<WorkspaceMember | null> {
    return await this.get<WorkspaceMember | null>(
      `/workspaces/${workspaceId}/members/transfer-ownership/pending`,
      { userId },
    );
  }

  private async patch<T>(
    endpoint: string,
    data: Record<string, unknown>,
    authOptions?: { useAdmin?: boolean; userId?: string },
  ): Promise<T> {
    return await this.fetchWithApiUrl<T>(
      endpoint,
      {
        method: "PATCH",
        body: JSON.stringify(data),
      },
      authOptions,
    );
  }

  // Deployment Methods

  async getDeploymentStatus(
    userId: string,
    deploymentId: string,
  ): Promise<DeploymentStatusResponse> {
    return await this.get<DeploymentStatusResponse>(
      `/deployments/${deploymentId}/status`,
      { userId },
    );
  }

  // Usage Methods

  async getAggregatedUsage(
    userId: string,
    startDate?: string,
    endDate?: string,
  ): Promise<AggregatedUsageResponse> {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get<AggregatedUsageResponse>(
      `/workspaces/usage/all${queryString}`,
      { userId },
    );
  }

  async getAggregatedDailyUsage(
    userId: string,
    startDate?: string,
    endDate?: string,
    timezone?: string,
  ): Promise<AggregatedDailyUsageResponse> {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    if (timezone) params.append("timezone_str", timezone);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get<AggregatedDailyUsageResponse>(
      `/workspaces/usage/all/daily${queryString}`,
      { userId },
    );
  }

  async getDeploymentCostBreakdown(
    userId: string,
    deploymentId: string,
    startDate?: string,
    endDate?: string,
  ): Promise<WorkspaceCostBreakdownResponse> {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get<WorkspaceCostBreakdownResponse>(
      `/deployments/${deploymentId}/usage/breakdown${queryString}`,
      { userId },
    );
  }

  async getMeterPricing(): Promise<MeterPricingResponse> {
    return await this.get<MeterPricingResponse>("/billing/meter-pricing");
  }

  async acceptInvitation(
    userId: string,
    invitationId: string,
  ): Promise<{ success: boolean }> {
    return await this.post<{ success: boolean }>(
      `/invitations/${invitationId}/accept`,
      {},
      { userId },
    );
  }

  async getPendingInvitations(
    userId: string,
  ): Promise<{
    workspace_id: string;
    workspace_name: string;
    email: string;
    role: string;
    invited_by_name: string;
    expires_at: string;
    invitation_type?: string;
    invitation_id: string;
  }[]> {
    return await this.get<
      {
        workspace_id: string;
        workspace_name: string;
        email: string;
        role: string;
        invited_by_name: string;
        expires_at: string;
        invitation_type?: string;
        invitation_id: string;
      }[]
    >(`/invitations/pending`, { userId });
  }

  async declineInvitation(
    userId: string,
    invitationId: string,
  ): Promise<{ success: boolean }> {
    return await this.post<{ success: boolean }>(
      `/invitations/${invitationId}/decline`,
      {},
      { userId },
    );
  }
}

const lazycloudApi = new LazyCloudAPIClass();
export default lazycloudApi;

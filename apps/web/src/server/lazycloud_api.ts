import { env } from "@/env";
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

class LazyCloudAPIClass {
  private static adminApiKey = env.ADMIN_API_KEY || null;
  private apiUrl: string = env.API_URL || "http://localhost:8000";
  private static VERSION_PREFIX = env.API_PREFIX || "/v1";

  // Utility Methods
  private async fetchWithApiUrl<T>(
    endpoint: string,
    options?: RequestInit,
    authOptions?: { useAdmin?: boolean; accessToken?: string },
  ): Promise<T> {
    const headers = new Headers(options?.headers);

    if (options?.body) {
      headers.set("Content-Type", "application/json");
    }

    // Determine which auth token to use
    const useAdmin = authOptions?.useAdmin ?? false;
    const token = useAdmin
      ? LazyCloudAPIClass.adminApiKey
      : authOptions?.accessToken ?? null;

    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    } else if (authOptions !== undefined && (authOptions.accessToken !== undefined || authOptions.useAdmin)) {
      // Only require auth if accessToken or useAdmin was explicitly provided
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
    authOptions?: { useAdmin?: boolean; accessToken?: string },
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
    authOptions?: { useAdmin?: boolean; accessToken?: string },
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

  private async delete<T>(
    endpoint: string,
    authOptions?: { useAdmin?: boolean; accessToken?: string },
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

  // Workspace Methods

  async getWorkspaces(
    accessToken: string,
    startDate?: string,
    endDate?: string,
  ): Promise<Workspace[]> {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get<Workspace[]>(`/workspaces${queryString}`, { accessToken });
  }

  async getWorkspace(accessToken: string, workspaceId: string): Promise<Workspace> {
    const workspaces = await this.getWorkspaces(accessToken);
    const workspace = workspaces.find((w) => w.id === workspaceId);
    if (!workspace) {
      throw new Error("Workspace not found");
    }
    return workspace;
  }

  async getWorkspaceWithDeployments(
    accessToken: string,
    workspaceId: string,
  ): Promise<WorkspaceWithDeploymentsResponse> {
    return await this.get<WorkspaceWithDeploymentsResponse>(
      `/workspaces/${workspaceId}/with-deployments`,
      { accessToken },
    );
  }

  async createWorkspace(accessToken: string, name: string): Promise<Workspace> {
    const data = {
      name: name,
    };
    return await this.post<Workspace>(`/workspaces`, data, { accessToken });
  }

  async deleteWorkspace(
    accessToken: string,
    workspaceId: string,
  ): Promise<{ success: boolean }> {
    return await this.delete<{ success: boolean }>(
      `/workspaces/${workspaceId}`,
      { accessToken },
    );
  }

  async leaveWorkspace(
    accessToken: string,
    workspaceId: string,
  ): Promise<{ success: boolean }> {
    return await this.post<{ success: boolean }>(
      `/workspaces/${workspaceId}/members/leave`,
      {},
      { accessToken },
    );
  }

  async getCurrentUser(accessToken: string): Promise<{ id: string }> {
    return await this.get<{ id: string }>(`/users/current`, { accessToken });
  }

  async getUserFeatures(accessToken: string): Promise<UserFeaturesResponse> {
    return await this.get<UserFeaturesResponse>(`/users/features`, { accessToken });
  }

  async getWorkspaceMembers(
    accessToken: string,
    workspaceId: string,
  ): Promise<WorkspaceMember[]> {
    const response = await this.get<WorkspaceMember[]>(
      `/workspaces/${workspaceId}/members`,
      { accessToken },
    );
    return response.map((member) => WorkspaceMemberSchema.parse(member));
  }

  async inviteUser(
    accessToken: string,
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
      { accessToken },
    );
  }

  async updateMemberRole(
    accessToken: string,
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
      { accessToken },
    );
  }

  async transferOwnership(
    accessToken: string,
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
      { accessToken },
    );
  }

  async removeMember(
    accessToken: string,
    workspaceId: string,
    memberUserId: string,
  ): Promise<{ success: boolean }> {
    return await this.delete<{ success: boolean }>(
      `/workspaces/${workspaceId}/members/${memberUserId}`,
      { accessToken },
    );
  }

  async cancelInvitation(
    accessToken: string,
    workspaceId: string,
    invitationId: string,
  ): Promise<{ success: boolean }> {
    return await this.delete<{ success: boolean }>(
      `/workspaces/${workspaceId}/members/invitations/${invitationId}`,
      { accessToken },
    );
  }

  async getPendingOwnershipTransfer(
    accessToken: string,
    workspaceId: string,
  ): Promise<WorkspaceMember | null> {
    return await this.get<WorkspaceMember | null>(
      `/workspaces/${workspaceId}/members/transfer-ownership/pending`,
      { accessToken },
    );
  }

  private async patch<T>(
    endpoint: string,
    data: Record<string, unknown>,
    authOptions?: { useAdmin?: boolean; accessToken?: string },
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
    accessToken: string,
    deploymentId: string,
  ): Promise<DeploymentStatusResponse> {
    return await this.get<DeploymentStatusResponse>(
      `/deployments/${deploymentId}/status`,
      { accessToken },
    );
  }

  // Usage Methods

  async getAggregatedUsage(
    accessToken: string,
    startDate?: string,
    endDate?: string,
  ): Promise<AggregatedUsageResponse> {
    const params = new URLSearchParams();
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    const queryString = params.toString() ? `?${params.toString()}` : "";
    return await this.get<AggregatedUsageResponse>(
      `/workspaces/usage/all${queryString}`,
      { accessToken },
    );
  }

  async getAggregatedDailyUsage(
    accessToken: string,
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
      { accessToken },
    );
  }

  async getDeploymentCostBreakdown(
    accessToken: string,
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
      { accessToken },
    );
  }

  async getMeterPricing(): Promise<MeterPricingResponse> {
    return await this.get<MeterPricingResponse>("/billing/meter-pricing");
  }

  async acceptInvitation(
    accessToken: string,
    invitationId: string,
  ): Promise<{ success: boolean }> {
    return await this.post<{ success: boolean }>(
      `/invitations/${invitationId}/accept`,
      {},
      { accessToken },
    );
  }

  async getPendingInvitations(
    accessToken: string,
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
    >(`/invitations/pending`, { accessToken });
  }

  async declineInvitation(
    accessToken: string,
    invitationId: string,
  ): Promise<{ success: boolean }> {
    return await this.post<{ success: boolean }>(
      `/invitations/${invitationId}/decline`,
      {},
      { accessToken },
    );
  }
}

const lazycloudApi = new LazyCloudAPIClass();
export default lazycloudApi;

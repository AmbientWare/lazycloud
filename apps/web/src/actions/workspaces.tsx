"use server";

import type {
  Workspace,
  WorkspaceMember,
  WorkspaceRole,
  WorkspaceWithDeploymentsResponse,
} from "@/interfaces/workspaces";
import lazycloudApi from "@/server/lazycloud_api";
import { getAuthToken } from "./utils";
import { env } from "@/env";

export async function getWorkspaces(
  startDate?: string,
  endDate?: string,
): Promise<Workspace[]> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getWorkspaces(accessToken, startDate, endDate);
}

export async function getWorkspaceWithDeployments(
  workspaceId: string,
): Promise<WorkspaceWithDeploymentsResponse> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getWorkspaceWithDeployments(accessToken, workspaceId);
}

export async function createWorkspace(name: string): Promise<Workspace> {
  const accessToken = await getAuthToken();
  return lazycloudApi.createWorkspace(accessToken, name);
}

export async function deleteWorkspace(
  workspaceId: string,
): Promise<{ success: boolean }> {
  const accessToken = await getAuthToken();
  return lazycloudApi.deleteWorkspace(accessToken, workspaceId);
}

export async function leaveWorkspace(
  workspaceId: string,
): Promise<{ success: boolean }> {
  const accessToken = await getAuthToken();
  return lazycloudApi.leaveWorkspace(accessToken, workspaceId);
}

export async function getWorkspaceMembers(
  workspaceId: string,
): Promise<WorkspaceMember[]> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getWorkspaceMembers(accessToken, workspaceId);
}

export async function inviteUser(
  workspaceId: string,
  email: string,
  role: WorkspaceRole = "member",
): Promise<{ success: boolean }> {
  const accessToken = await getAuthToken();
  const acceptanceUrl = `${env.APP_URL}/workspaces`;

  return lazycloudApi.inviteUser(accessToken, workspaceId, email, role, acceptanceUrl);
}

export async function updateMemberRole(
  workspaceId: string,
  memberUserId: string,
  role: WorkspaceRole,
): Promise<WorkspaceMember> {
  const accessToken = await getAuthToken();
  return lazycloudApi.updateMemberRole(accessToken, workspaceId, memberUserId, role);
}

export async function removeMember(
  workspaceId: string,
  memberUserId: string,
): Promise<{ success: boolean }> {
  const accessToken = await getAuthToken();
  return lazycloudApi.removeMember(accessToken, workspaceId, memberUserId);
}

export async function transferOwnership(
  workspaceId: string,
  newOwnerUserId: string,
): Promise<{ success: boolean }> {
  const accessToken = await getAuthToken();
  const acceptanceUrl = `${env.APP_URL}/workspaces`;
  return lazycloudApi.transferOwnership(accessToken, workspaceId, newOwnerUserId, acceptanceUrl);
}

export async function acceptInvitation(
  invitationId: string,
): Promise<{ success: boolean }> {
  const accessToken = await getAuthToken();
  return lazycloudApi.acceptInvitation(accessToken, invitationId);
}

export async function declineInvitation(
  invitationId: string,
): Promise<{ success: boolean }> {
  const accessToken = await getAuthToken();
  return lazycloudApi.declineInvitation(accessToken, invitationId);
}

export async function getPendingInvitations(): Promise<{
  workspace_id: string;
  workspace_name: string;
  email: string;
  role: string;
  invited_by_name: string;
  expires_at: string;
  invitation_type?: string;
  invitation_id: string;
}[]> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getPendingInvitations(accessToken);
}

export async function cancelInvitation(
  workspaceId: string,
  invitationId: string,
): Promise<{ success: boolean }> {
  const accessToken = await getAuthToken();
  return lazycloudApi.cancelInvitation(accessToken, workspaceId, invitationId);
}

export async function getPendingOwnershipTransfer(
  workspaceId: string,
): Promise<WorkspaceMember | null> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getPendingOwnershipTransfer(accessToken, workspaceId);
}

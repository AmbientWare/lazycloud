"use server";

import type {
  Workspace,
  WorkspaceMember,
  WorkspaceRole,
  WorkspaceWithDeploymentsResponse,
} from "@/interfaces/workspaces";
import lazycloudApi from "@/server/lazycloud_api";
import { getUserId } from "./utils";
import { env } from "@/env";

export async function getWorkspaces(
  startDate?: string,
  endDate?: string,
): Promise<Workspace[]> {
  const userId = await getUserId();
  return lazycloudApi.getWorkspaces(userId, startDate, endDate);
}

export async function getWorkspaceWithDeployments(
  workspaceId: string,
): Promise<WorkspaceWithDeploymentsResponse> {
  const userId = await getUserId();
  return lazycloudApi.getWorkspaceWithDeployments(userId, workspaceId);
}

export async function createWorkspace(name: string): Promise<Workspace> {
  const userId = await getUserId();
  return lazycloudApi.createWorkspace(userId, name);
}

export async function deleteWorkspace(
  workspaceId: string,
): Promise<{ success: boolean }> {
  const userId = await getUserId();
  return lazycloudApi.deleteWorkspace(userId, workspaceId);
}

export async function leaveWorkspace(
  workspaceId: string,
): Promise<{ success: boolean }> {
  const userId = await getUserId();
  return lazycloudApi.leaveWorkspace(userId, workspaceId);
}

export async function getWorkspaceMembers(
  workspaceId: string,
): Promise<WorkspaceMember[]> {
  const userId = await getUserId();
  return lazycloudApi.getWorkspaceMembers(userId, workspaceId);
}

export async function inviteUser(
  workspaceId: string,
  email: string,
  role: WorkspaceRole = "member",
): Promise<{ success: boolean }> {
  const userId = await getUserId();
  const acceptanceUrl = `${env.APP_URL}/workspaces`;
  
  return lazycloudApi.inviteUser(userId, workspaceId, email, role, acceptanceUrl);
}

export async function updateMemberRole(
  workspaceId: string,
  memberUserId: string,
  role: WorkspaceRole,
): Promise<WorkspaceMember> {
  const userId = await getUserId();
  return lazycloudApi.updateMemberRole(userId, workspaceId, memberUserId, role);
}

export async function removeMember(
  workspaceId: string,
  memberUserId: string,
): Promise<{ success: boolean }> {
  const userId = await getUserId();
  return lazycloudApi.removeMember(userId, workspaceId, memberUserId);
}

export async function transferOwnership(
  workspaceId: string,
  newOwnerUserId: string,
): Promise<{ success: boolean }> {
  const userId = await getUserId();
  const acceptanceUrl = `${env.APP_URL}/workspaces`;
  return lazycloudApi.transferOwnership(userId, workspaceId, newOwnerUserId, acceptanceUrl);
}

export async function acceptInvitation(
  invitationId: string,
): Promise<{ success: boolean }> {
  const userId = await getUserId();
  return lazycloudApi.acceptInvitation(userId, invitationId);
}

export async function declineInvitation(
  invitationId: string,
): Promise<{ success: boolean }> {
  const userId = await getUserId();
  return lazycloudApi.declineInvitation(userId, invitationId);
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
  const userId = await getUserId();
  return lazycloudApi.getPendingInvitations(userId);
}

export async function cancelInvitation(
  workspaceId: string,
  invitationId: string,
): Promise<{ success: boolean }> {
  const userId = await getUserId();
  return lazycloudApi.cancelInvitation(userId, workspaceId, invitationId);
}

export async function getPendingOwnershipTransfer(
  workspaceId: string,
): Promise<WorkspaceMember | null> {
  const userId = await getUserId();
  return lazycloudApi.getPendingOwnershipTransfer(userId, workspaceId);
}

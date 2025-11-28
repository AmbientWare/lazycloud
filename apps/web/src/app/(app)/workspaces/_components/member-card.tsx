"use client";

import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { StyledCard } from "@/components/shared/styled-card";
import { StyledTooltip } from "@/components/shared/styled-tooltip";
import { Shield, User, Trash2 } from "lucide-react";
import type { WorkspaceMember, WorkspaceRole } from "@/interfaces/workspaces";
import { canManageMembers, WorkspaceRoles } from "@/interfaces/workspaces";
import { updateMemberRole, removeMember } from "@/actions/workspaces";
import { ExpandConfirmButton } from "@/components/shared/expand-confirm-button";
import { useState } from "react";

interface MemberCardProps {
  member: WorkspaceMember;
  currentUserRole?: WorkspaceRole;
  currentUserId?: string;
  workspaceId: string;
  onUpdate?: () => void;
  isLoading?: boolean;
}

const getRoleColor = (role: WorkspaceRole) => {
  switch (role) {
    case WorkspaceRoles.OWNER:
      return "bg-purple-500/10 text-purple-500 border-purple-500/30";
    case WorkspaceRoles.ADMIN:
      return "bg-blue-500/10 text-blue-500 border-blue-500/30";
    case WorkspaceRoles.MEMBER:
      return "bg-green-500/10 text-green-500 border-green-500/30";
    default:
      return "";
  }
};

export function MemberCard({
  member,
  currentUserRole,
  currentUserId,
  workspaceId,
  onUpdate,
  isLoading = false,
}: MemberCardProps) {
  const [isUpdating, setIsUpdating] = useState(false);

  const canManage = canManageMembers(currentUserRole);
  const isOwner = member.role === WorkspaceRoles.OWNER;
  const isCurrentUser = member.user_id === currentUserId;
  const isInvited = member.status === "invited";
  const canRemove = canManage && !isOwner && !isCurrentUser;
  const canChangeRole = canManage && !isOwner && !isCurrentUser && !isInvited;

  const handleRoleChange = async (newRole: WorkspaceRole) => {
    if (newRole === member.role || !member.user_id) return;
    setIsUpdating(true);
    try {
      await updateMemberRole(workspaceId, member.user_id, newRole);
      onUpdate?.();
    } catch (error) {
      console.error("Failed to update member role:", error);
    } finally {
      setIsUpdating(false);
    }
  };

  const handleRemove = async () => {
    try {
      if (member.user_id) {
        await removeMember(workspaceId, member.user_id);
      }
      onUpdate?.();
    } catch (error) {
      console.error("Failed to remove member:", error);
    }
  };

  if (isLoading) {
    return (
      <StyledCard
        variant="minimal"
        className="border-muted gap-0 overflow-hidden p-4"
      >
        <div className="flex items-center justify-between gap-3">
          <Skeleton className="h-5 w-64" />
          <Skeleton className="h-8 w-24" />
        </div>
      </StyledCard>
    );
  }

  const hasName = member.name?.trim();
  
  return (
    <StyledCard
      variant="minimal"
      className="border-border/50 bg-muted/30 gap-0 overflow-hidden shadow-sm p-4"
    >
      <div className="flex items-center justify-between gap-3 min-h-[32px]">
        {/* Left side: Name/Email */}
        <div className="flex items-center gap-1.5 min-w-0 flex-1">
          {member.role === WorkspaceRoles.OWNER && (
            <Shield className="h-4 w-4 shrink-0 text-purple-500" />
          )}
          {member.role === WorkspaceRoles.ADMIN && (
            <User className="h-4 w-4 shrink-0 text-blue-500" />
          )}
          <div className="truncate text-sm">
            {hasName ? (
              <>
                <span className="font-medium">{member.name}</span>
                <span className="text-muted-foreground"> · {member.email}</span>
              </>
            ) : (
              <span className="font-medium">{member.email}</span>
            )}
          </div>
        </div>

        {/* Right side: Badges and actions */}
        <div className="flex items-center gap-2 shrink-0">
          {isCurrentUser && (
            <Badge variant="outline" className="text-xs shrink-0">
              You
            </Badge>
          )}
          {canChangeRole ? (
            <Select
              value={member.role}
              onValueChange={(value) =>
                handleRoleChange(value as WorkspaceRole)
              }
              disabled={isUpdating}
            >
              <SelectTrigger className="h-8 w-[120px] text-xs cursor-pointer">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={WorkspaceRoles.MEMBER} className="cursor-pointer">
                  <div className="flex items-center gap-2">
                    <User className="h-3 w-3" />
                    Member
                  </div>
                </SelectItem>
                <SelectItem value={WorkspaceRoles.ADMIN} className="cursor-pointer">
                  <div className="flex items-center gap-2">
                    <Shield className="h-3 w-3" />
                    Admin
                  </div>
                </SelectItem>
              </SelectContent>
            </Select>
          ) : (
            <Badge
              variant="outline"
              className={`text-xs capitalize ${getRoleColor(member.role)}`}
            >
              {member.role}
            </Badge>
          )}
          {canRemove && (
            <StyledTooltip content="Remove member">
              <div>
                <ExpandConfirmButton
                  onConfirm={handleRemove}
                  icon={Trash2}
                  color="red"
                  buttonClassName="text-red-500 hover:bg-red-500/10 cursor-pointer"
                />
              </div>
            </StyledTooltip>
          )}
        </div>
      </div>
    </StyledCard>
  );
}

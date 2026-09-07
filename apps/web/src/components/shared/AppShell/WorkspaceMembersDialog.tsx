import { useState } from "react";
import { ConfirmAction } from "@/components/shared/ConfirmAction";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Loader2, MailPlus, RefreshCw, X } from "lucide-react";
import { toast } from "sonner";

import { useSession } from "@/components/shared/AuthGate/session";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogBody,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type {
  EmailDelivery,
  InvitableRole,
  Workspace,
  WorkspaceInvitation,
  WorkspaceMember,
} from "@/lib/api/schemas";
import { usagePhrase } from "@/lib/entitlements";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import {
  inviteWorkspaceMember,
  removeWorkspaceMember,
  resendWorkspaceInvitation,
  revokeWorkspaceInvitation,
  setWorkspaceMemberRole,
  workspaceInvitationsQueryOptions,
  workspaceMembersQueryOptions,
} from "@/lib/queries/members";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { useWorkspaceSelection } from "@/lib/workspace-selection";

/**
 * What the server says became of the invitation email.
 *
 * Only outcomes worth acting on get a line. Queued and sent both mean the
 * platform did its part and nobody has reported back, which is the ordinary
 * state and not worth putting in front of anyone.
 */
const DELIVERY_NOTES: Partial<Record<EmailDelivery, string>> = {
  bounced: "Email bounced",
  complained: "Marked as spam",
  failed: "Email could not be sent",
};

const ROLE_LABELS: Record<WorkspaceMember["role"], string> = {
  owner: "Owner",
  administrator: "Administrator",
  member: "Member",
};

/**
 * Who reaches a workspace, and the offers still waiting on an answer.
 *
 * An owner or administrator invites by email, changes roles, revokes and resends
 * invitations, and removes members. Everyone else reads the list and can leave.
 * Ownership is not editable here: it moves by transfer, not by picking a role.
 */
export function WorkspaceMembersDialog({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const { user } = useSession();
  const members = useQuery(workspaceMembersQueryOptions(workspace.id, workspace.name));
  const me = members.data?.data.find((member) => member.user_id === user.id);
  const manages = me?.role === "owner" || me?.role === "administrator";
  const owner = me?.role === "owner";
  const invitations = useQuery({
    ...workspaceInvitationsQueryOptions(workspace.id, workspace.name),
    enabled: manages,
  });
  const billing = useQuery({ ...billingSummaryQueryOptions(), enabled: owner });

  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="flex max-w-lg flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>{workspace.name} members</DialogTitle>
          <DialogDescription>People who can access this workspace.</DialogDescription>
        </DialogHeader>
        {owner && billing.data?.entitlements ? (
          <p className="rounded-md border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
            {usagePhrase(
              billing.data.usage.members,
              billing.data.entitlements.max_members,
              "account member",
            )}
          </p>
        ) : null}
        {manages ? <InviteForm workspace={workspace} /> : null}
        <DialogBody>
          {members.isPending ? (
            <div className="space-y-2" aria-hidden="true">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : members.error ? (
            <p className="text-sm text-destructive" role="alert">
              {members.error.message}
            </p>
          ) : (
            <ul className="divide-y divide-border border-y border-border">
              {(members.data?.data ?? []).map((member) => (
                <MemberRow
                  key={member.user_id}
                  workspace={workspace}
                  member={member}
                  self={member.user_id === user.id}
                  manages={manages}
                  onLeft={onClose}
                />
              ))}
              {manages
                ? (invitations.data?.data ?? []).map((invitation) => (
                    <InvitationRow
                      key={invitation.id}
                      workspace={workspace}
                      invitation={invitation}
                    />
                  ))
                : null}
            </ul>
          )}
          {manages && invitations.error ? (
            <p className="text-sm text-destructive" role="alert">
              {invitations.error.message}
            </p>
          ) : null}
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}

function InviteForm({ workspace }: { workspace: Workspace }) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<InvitableRole>("member");
  const invite = useMutation({
    mutationFn: () => inviteWorkspaceMember(workspace.name, email.trim(), role),
    onSuccess: (invitation) => {
      setEmail("");
      toast.success(`Invitation sent to ${invitation.email}`);
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.invitations(workspace.id),
      });
    },
    onError: (error) => {
      // A 503 here means the row exists and the email did not go out; the list
      // refetch is what surfaces it with a resend action.
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.invitations(workspace.id),
      });
      toast.error("Could not invite", { description: error.message });
    },
  });
  const valid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());

  return (
    <form
      className="flex flex-col gap-2 sm:flex-row"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid && !invite.isPending) invite.mutate();
      }}
    >
      <Input
        aria-label="Email address to invite"
        autoComplete="off"
        disabled={invite.isPending}
        onChange={(event) => setEmail(event.target.value)}
        placeholder="name@example.com"
        type="email"
        value={email}
      />
      <RoleSelect
        aria-label="Role for the invited person"
        disabled={invite.isPending}
        onChange={setRole}
        value={role}
      />
      <Button disabled={!valid || invite.isPending} type="submit" className="shrink-0">
        {invite.isPending ? (
          <Loader2 className="size-4 animate-spin" />
        ) : (
          <MailPlus className="size-4" />
        )}
        Invite
      </Button>
    </form>
  );
}

function RoleSelect({
  value,
  onChange,
  disabled,
  "aria-label": ariaLabel,
}: {
  value: InvitableRole;
  onChange: (role: InvitableRole) => void;
  disabled?: boolean;
  "aria-label": string;
}) {
  return (
    <Select
      disabled={disabled}
      onValueChange={(next) => onChange(next === "administrator" ? "administrator" : "member")}
      value={value}
    >
      <SelectTrigger aria-label={ariaLabel} className="w-full sm:w-40">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="member">Member</SelectItem>
        <SelectItem value="administrator">Administrator</SelectItem>
      </SelectContent>
    </Select>
  );
}

function MemberRow({
  workspace,
  member,
  self,
  manages,
  onLeft,
}: {
  workspace: Workspace;
  member: WorkspaceMember;
  self: boolean;
  manages: boolean;
  onLeft: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const forgetWorkspaceName = useWorkspaceSelection((state) => state.forgetWorkspaceName);
  const setRole = useMutation({
    mutationFn: (role: InvitableRole) =>
      setWorkspaceMemberRole(workspace.name, member.user_id, role),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.members(workspace.id) }),
    onError: (error) => toast.error("Could not change role", { description: error.message }),
  });
  const remove = useMutation({
    mutationFn: () => removeWorkspaceMember(workspace.name, member.user_id),
    onSuccess: async () => {
      if (self) {
        // Leave the workspace's own pages first. Refetching the session while the
        // shell is still mounted here would fire every workspace-scoped query
        // against a workspace this account no longer reaches, and paint their
        // 403s on the way out.
        onLeft();
        forgetWorkspaceName(workspace.name);
        await navigate({ to: "/dashboard" });
        await queryClient.invalidateQueries({ queryKey: currentSessionQueryOptions().queryKey });
        return;
      }
      await queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.members(workspace.id) });
    },
    onError: (error) =>
      toast.error(self ? "Could not leave" : "Could not remove member", {
        description: error.message,
      }),
  });
  const label = member.display_name || member.email;
  // The owner leaves by transferring the workspace, so neither control is theirs.
  const editable = manages && member.role !== "owner" && !self;
  const removable = member.role !== "owner" && (self || manages);

  return (
    <li className="flex items-center justify-between gap-3 py-3">
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium">
          {label}
          {self ? <span className="ml-1 text-xs text-muted-foreground">(you)</span> : null}
        </span>
        {member.email ? (
          <span className="block truncate text-xs text-muted-foreground">{member.email}</span>
        ) : null}
      </span>
      <span className="flex shrink-0 items-center gap-2">
        {editable ? (
          <RoleSelect
            aria-label={`Role for ${label}`}
            disabled={setRole.isPending}
            onChange={(role) => setRole.mutate(role)}
            value={member.role === "administrator" ? "administrator" : "member"}
          />
        ) : (
          <span className="text-xs text-muted-foreground">{ROLE_LABELS[member.role]}</span>
        )}
        {removable ? (
          <ConfirmAction
            title={self ? `Leave ${workspace.name}?` : `Remove ${label}?`}
            description={
              self
                ? "You will lose access to this workspace. A workspace administrator must invite you to join again."
                : `This person will lose access to ${workspace.name}.`
            }
            label={self ? "Leave workspace" : "Remove member"}
            onConfirm={async () => {
              await remove.mutateAsync();
            }}
          >
            <Button
              aria-label={self ? "Leave this workspace" : `Remove ${label}`}
              disabled={remove.isPending}
              size={self ? "sm" : "icon"}
              type="button"
              variant={self ? "outline" : "ghost"}
            >
              {remove.isPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : self ? null : (
                <X className="size-4" />
              )}
              {self ? "Leave" : null}
            </Button>
          </ConfirmAction>
        ) : null}
      </span>
    </li>
  );
}

function InvitationRow({
  workspace,
  invitation,
}: {
  workspace: Workspace;
  invitation: WorkspaceInvitation;
}) {
  const queryClient = useQueryClient();
  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.invitations(workspace.id) });
  const resend = useMutation({
    mutationFn: () => resendWorkspaceInvitation(workspace.name, invitation.id),
    onSuccess: () => {
      toast.success(`Invitation resent to ${invitation.email}`);
      return refresh();
    },
    onError: (error) => toast.error("Could not resend", { description: error.message }),
  });
  const revoke = useMutation({
    mutationFn: () => revokeWorkspaceInvitation(workspace.name, invitation.id),
    onSuccess: refresh,
    onError: (error) => toast.error("Could not revoke", { description: error.message }),
  });
  const busy = resend.isPending || revoke.isPending;

  return (
    <li className="flex items-center justify-between gap-3 py-3">
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium">
          {invitation.email}
          {DELIVERY_NOTES[invitation.delivery] ? (
            <span className="ml-2 text-xs font-normal text-destructive">needs attention</span>
          ) : null}
        </span>
        <span className="block truncate text-xs text-muted-foreground">
          {DELIVERY_NOTES[invitation.delivery] ??
            (invitation.expired ? "Invitation expired" : "Invited")}
          {invitation.invited_by_name ? ` by ${invitation.invited_by_name}` : ""}
          {" · "}
          {ROLE_LABELS[invitation.role]}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1">
        <Button
          aria-label={`Resend the invitation to ${invitation.email}`}
          disabled={busy}
          onClick={() => resend.mutate()}
          size="icon"
          type="button"
          variant="ghost"
        >
          {resend.isPending ? (
            <Loader2 className="size-4 animate-spin" />
          ) : (
            <RefreshCw className="size-4" />
          )}
        </Button>
        <Button
          aria-label={`Revoke the invitation to ${invitation.email}`}
          disabled={busy}
          onClick={() => revoke.mutate()}
          size="icon"
          type="button"
          variant="ghost"
        >
          {revoke.isPending ? (
            <Loader2 className="size-4 animate-spin" />
          ) : (
            <X className="size-4" />
          )}
        </Button>
      </span>
    </li>
  );
}

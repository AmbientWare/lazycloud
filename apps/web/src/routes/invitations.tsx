import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import { useSession } from "@/components/shared/AuthGate/session";
import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { PendingInvitation } from "@/lib/api/schemas";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import {
  acceptInvitation,
  declineInvitation,
  pendingInvitationsQueryOptions,
} from "@/lib/queries/members";

/**
 * Where an invitation email sends you.
 *
 * Outside the workspace shell on purpose: the person arriving may reach no
 * workspace but their own yet, and the list is keyed to their account rather than
 * to any workspace. What shows is whatever is addressed to the email GitHub
 * verified for the signed-in account, so a forwarded link shows nothing.
 */
export const Route = createFileRoute("/invitations")({
  component: InvitationsPage,
  head: () => ({
    meta: [{ title: "Invitations | LazyCloud" }],
  }),
});

function InvitationsPage() {
  const { user } = useSession();
  const pending = useQuery(pendingInvitationsQueryOptions());

  return (
    <PreShellScreen width="lg">
      <div className="mb-4">
        <h1 className="text-xl font-semibold">Invitations</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {user.email
            ? `Workspaces that invited ${user.email}.`
            : "This account has no verified email, so no invitation can be addressed to it."}
        </p>
      </div>
      {pending.isPending ? (
        <div className="space-y-2" aria-hidden="true">
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-14 w-full" />
        </div>
      ) : pending.error ? (
        <p className="text-sm text-destructive" role="alert">
          {pending.error.message}
        </p>
      ) : pending.data.data.length === 0 ? (
        <p className="rounded-md border border-border bg-muted/30 px-3 py-4 text-sm text-muted-foreground">
          Nothing is waiting for you.
        </p>
      ) : (
        <ul className="divide-y divide-border border-y border-border">
          {pending.data.data.map((invitation) => (
            <InvitationRow key={invitation.id} invitation={invitation} />
          ))}
        </ul>
      )}
      <div className="mt-5">
        <Button asChild variant="outline">
          <Link to="/dashboard">Go to the dashboard</Link>
        </Button>
      </div>
    </PreShellScreen>
  );
}

function InvitationRow({ invitation }: { invitation: PendingInvitation }) {
  const queryClient = useQueryClient();
  const settle = async () => {
    // Accepting changes which workspaces the session reaches, so both the
    // account's list and the session are re-read.
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: pendingInvitationsQueryOptions().queryKey }),
      queryClient.invalidateQueries({ queryKey: currentSessionQueryOptions().queryKey }),
    ]);
  };
  const accept = useMutation({
    mutationFn: () => acceptInvitation(invitation.id),
    onSuccess: async () => {
      toast.success(`You joined ${invitation.workspace_name}`);
      await settle();
    },
    onError: (error) => toast.error("Could not accept", { description: error.message }),
  });
  const decline = useMutation({
    mutationFn: () => declineInvitation(invitation.id),
    onSuccess: settle,
    onError: (error) => toast.error("Could not decline", { description: error.message }),
  });
  const busy = accept.isPending || decline.isPending;
  const role = invitation.role === "administrator" ? "an administrator" : "a member";

  return (
    <li className="flex flex-col gap-3 py-3 sm:flex-row sm:items-center sm:justify-between">
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium">{invitation.workspace_name}</span>
        <span className="block text-xs text-muted-foreground">
          {invitation.invited_by_name ? `${invitation.invited_by_name} invited you` : "Invited"} as{" "}
          {role}. Expires {new Date(invitation.expires_at).toLocaleDateString()}.
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-2">
        <Button disabled={busy} onClick={() => decline.mutate()} size="sm" variant="outline">
          {decline.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
          Decline
        </Button>
        <Button disabled={busy} onClick={() => accept.mutate()} size="sm">
          {accept.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
          Accept
        </Button>
      </span>
    </li>
  );
}

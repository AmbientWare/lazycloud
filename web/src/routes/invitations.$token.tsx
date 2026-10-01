import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import { AuthGate } from "@/components/shared/AuthGate";
import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import {
  acceptInvitation,
  declineInvitation,
  invitationPreviewQueryOptions,
} from "@/lib/queries/members";

/**
 * Where an invitation link lands.
 *
 * Outside the workspace shell, because whoever arrives may reach no workspace
 * yet, and the offer belongs to the link rather than to any workspace they can
 * already see. The auth gate sends a signed-out visitor to sign in and back
 * here, so the link works whether or not they had an account when they clicked.
 *
 * Opening this page never redeems the offer. Accepting is a button, so a mail
 * scanner following the URL cannot join a workspace on somebody's behalf.
 */
export const Route = createFileRoute("/invitations/$token")({
  component: () => (
    <AuthGate>
      <InvitationPage />
    </AuthGate>
  ),
  head: () => ({
    meta: [{ title: "Invitation | LazyCloud" }],
  }),
});

function InvitationPage() {
  const { token } = Route.useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const preview = useQuery(invitationPreviewQueryOptions(token));

  const settle = async () => {
    // Accepting changes which workspaces the session reaches, so it has to be
    // re-read before anything navigates into the new one.
    await queryClient.invalidateQueries({ queryKey: currentSessionQueryOptions().queryKey });
  };

  const accept = useMutation({
    mutationFn: () => acceptInvitation(token),
    onSuccess: async () => {
      const name = preview.data?.workspace_name;
      toast.success(name ? `You joined ${name}` : "Invitation accepted");
      await settle();
      await navigate(
        name ? { to: "/w/$workspace", params: { workspace: name } } : { to: "/dashboard" },
      );
    },
    onError: (error) => toast.error("Could not accept", { description: error.message }),
  });

  const decline = useMutation({
    mutationFn: () => declineInvitation(token),
    onSuccess: async () => {
      toast.success("Invitation declined");
      await navigate({ to: "/dashboard" });
    },
    onError: (error) => toast.error("Could not decline", { description: error.message }),
  });

  const busy = accept.isPending || decline.isPending;

  return (
    <PreShellScreen>
      {preview.isPending ? (
        <div className="space-y-3" aria-hidden="true">
          <Skeleton className="h-6 w-2/3" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-9 w-full" />
        </div>
      ) : preview.error ? (
        <Unavailable error={preview.error} />
      ) : preview.data.expired ? (
        <>
          <h1 className="text-xl font-semibold">This invitation has expired</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Ask an administrator of {preview.data.workspace_name} to send it again.
          </p>
          <Button asChild className="mt-5 w-full" variant="outline">
            <a href="/dashboard">Go to the dashboard</a>
          </Button>
        </>
      ) : (
        <>
          <h1 className="text-xl font-semibold">Join {preview.data.workspace_name}</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {preview.data.invited_by_name
              ? `${preview.data.invited_by_name} invited ${preview.data.email}`
              : `An invitation was sent to ${preview.data.email}`}{" "}
            as {preview.data.role === "administrator" ? "an administrator" : "a member"}.
          </p>
          <p className="mt-3 rounded-md border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
            You will join as the account you are signed in as, which may not be the address this was
            sent to.
          </p>
          <div className="mt-5 flex gap-2">
            <Button
              className="flex-1"
              disabled={busy}
              onClick={() => decline.mutate()}
              variant="outline"
            >
              {decline.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
              Decline
            </Button>
            <Button className="flex-1" disabled={busy} onClick={() => accept.mutate()}>
              {accept.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
              Accept
            </Button>
          </div>
        </>
      )}
    </PreShellScreen>
  );
}

/**
 * A spent, withdrawn or mistyped link all answer 404 and read the same here.
 * They are the same fact to whoever is holding it: this link does not open
 * anything, and nothing it could say would tell them which it was.
 */
function Unavailable({ error }: { error: Error }) {
  const missing = error instanceof ApiError && error.status === 404;
  return (
    <>
      <h1 className="text-xl font-semibold">
        {missing ? "This invitation is no longer open" : "Could not open this invitation"}
      </h1>
      <p className="mt-2 text-sm text-muted-foreground">
        {missing
          ? "It may have been accepted, declined, withdrawn, or replaced by a newer one. Ask whoever invited you to send another."
          : error.message}
      </p>
      <Button asChild className="mt-5 w-full" variant="outline">
        <a href="/dashboard">Go to the dashboard</a>
      </Button>
    </>
  );
}

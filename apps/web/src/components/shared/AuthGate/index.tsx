import { useCallback, useMemo, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { Button } from "@/components/ui/button";
import { ApiError, clearAuthToken } from "@/lib/api/client";
import { useAuthToken } from "@/hooks/use-auth-token";
import { currentSessionQueryOptions, signOut } from "@/lib/queries/auth";
import { SessionContext, type SessionContextValue } from "@/components/shared/AuthGate/session";
import { SignInScreen } from "@/components/shared/AuthGate/SignInScreen";

export function AuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const token = useAuthToken();
  // One request answers both questions the shell needs: who is signed in, and which
  // workspaces they reach. Resolving them separately would let the two disagree.
  const session = useQuery({
    ...currentSessionQueryOptions(),
    enabled: !!token,
  });

  const logout = useCallback(() => {
    // The request names the session by the credential it carries, so it has to be
    // issued before the browser forgets that credential. A failure to reach the
    // server still signs the person out here; the session then ends at expiry.
    void signOut().catch(() => undefined);
    clearAuthToken();
    queryClient.clear();
    // Out to the public landing page rather than the sign-in screen. Signing out
    // is leaving, and being handed the way back in is the one thing somebody who
    // just left did not ask for; it also reads as though the sign-out failed.
    void navigate({ to: "/" });
  }, [navigate, queryClient]);

  const contextValue = useMemo<SessionContextValue | null>(
    () =>
      session.data
        ? {
            user: session.data.user,
            workspaces: session.data.workspaces,
            logout,
          }
        : null,
    [logout, session.data],
  );

  if (token === undefined) {
    return <LoadingScreen />;
  }

  if (!token) {
    return (
      <SignInScreen
        error={
          session.error instanceof ApiError && session.error.status === 401
            ? "Your session expired. Sign in again."
            : undefined
        }
      />
    );
  }

  if (session.isPending) {
    return <LoadingScreen />;
  }

  if (session.isError || !contextValue) {
    return (
      <PreShellScreen>
        <ApiErrorNotice
          title="Could not load your session"
          error={
            session.error instanceof ApiError
              ? session.error
              : new Error("Check your connection and try again.")
          }
          onRetry={() => void session.refetch()}
          retrying={session.isFetching}
        />
        {session.error instanceof ApiError && session.error.status === 403 ? (
          <Button variant="outline" onClick={logout}>
            Sign out
          </Button>
        ) : null}
      </PreShellScreen>
    );
  }

  return <SessionContext.Provider value={contextValue}>{children}</SessionContext.Provider>;
}

function LoadingScreen() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <Loader2 className="size-6 animate-spin text-brand" />
    </div>
  );
}

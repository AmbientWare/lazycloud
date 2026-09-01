import { useCallback, useMemo, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useRouterState } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { ApiError, clearAuthToken } from "@/lib/api/client";
import { useAuthToken } from "@/hooks/use-auth-token";
import { currentSessionQueryOptions, signOut } from "@/lib/queries/auth";
import { SessionContext, type SessionContextValue } from "@/components/shared/AuthGate/session";
import { SignInScreen } from "@/components/shared/AuthGate/SignInScreen";

// The public marketing pages, plus the two routes that run before there is a
// session to gate on. Gating `/callback` would drop the code it arrived to
// redeem, and gating `/signin` would hide the reason a sign-in failed behind the
// screen that failed to explain it.
const UNAUTHENTICATED_PATHS = new Set(["/", "/pricing", "/callback", "/signin"]);

export function AuthGate({ children }: { children: ReactNode }) {
  const unauthenticatedRoute = useRouterState({
    select: (state) => isUnauthenticatedPath(state.location.pathname),
  });
  return unauthenticatedRoute ? children : <AuthenticatedSession>{children}</AuthenticatedSession>;
}

function AuthenticatedSession({ children }: { children: ReactNode }) {
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
    return <SignInScreen />;
  }

  if (session.isPending) {
    return <LoadingScreen />;
  }

  if (session.isError || !contextValue) {
    return <SignInScreen error={loginErrorMessage(session.error)} />;
  }

  return <SessionContext.Provider value={contextValue}>{children}</SessionContext.Provider>;
}

function isUnauthenticatedPath(pathname: string): boolean {
  const normalized = pathname === "/" ? pathname : pathname.replace(/\/+$/, "");
  return UNAUTHENTICATED_PATHS.has(normalized);
}

function LoadingScreen() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <Loader2 className="size-6 animate-spin text-brand" />
    </div>
  );
}

function loginErrorMessage(error: unknown): string {
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
    return "Your session expired. Sign in again.";
  }
  if (error instanceof ApiError) {
    return `Sign-in failed (${error.status} ${error.statusText}).`;
  }
  if (error instanceof Error && error.message) {
    return "Cannot reach the LazyCloud API. Check that it is running, then retry.";
  }
  return "Could not validate the session.";
}

import { useCallback, useMemo, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouterState } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { ApiError, clearAuthToken } from "@/lib/api/client";
import { getStoredAuthToken } from "@/lib/auth";
import { currentSessionQueryOptions, signOut } from "@/lib/queries/auth";
import { SessionContext, type SessionContextValue } from "@/components/shared/AuthGate/session";
import { SignInScreen } from "@/components/shared/AuthGate/SignInScreen";

// `/callback` and `/signin` both run before there is a session to gate on. Gating
// `/callback` would drop the code it arrived to redeem, and gating `/signin` would
// hide the reason a sign-in failed behind the screen that failed to explain it.
const UNAUTHENTICATED_PATHS = new Set(["/", "/callback", "/signin"]);

export function AuthGate({ children }: { children: ReactNode }) {
  const unauthenticatedRoute = useRouterState({
    select: (state) => isUnauthenticatedPath(state.location.pathname),
  });
  return unauthenticatedRoute ? children : <AuthenticatedSession>{children}</AuthenticatedSession>;
}

function AuthenticatedSession({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [token, setToken] = useState<string | null>(() => getStoredAuthToken());
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
    setToken(null);
    queryClient.clear();
  }, [queryClient]);

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
    return `The control plane rejected the request (${error.status} ${error.statusText}).`;
  }
  if (error instanceof Error && error.message) {
    return "The control plane is unreachable. Check that the API is running, then retry.";
  }
  return "The session could not be validated.";
}

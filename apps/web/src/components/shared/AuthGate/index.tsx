import { useCallback, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouterState } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiError, clearAuthToken, setAuthToken } from "@/lib/api/client";
import { getStoredAuthToken } from "@/lib/auth";
import { currentSessionQueryOptions, signInMutationOptions, signOut } from "@/lib/queries/auth";
import { SessionContext, type SessionContextValue } from "@/components/shared/AuthGate/session";

const PUBLIC_MARKETING_PATHS = new Set(["/"]);

export function AuthGate({ children }: { children: ReactNode }) {
  const publicMarketingRoute = useRouterState({
    select: (state) => isPublicMarketingPath(state.location.pathname),
  });
  return publicMarketingRoute ? children : <AuthenticatedSession>{children}</AuthenticatedSession>;
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

  const onSignedIn = useCallback(
    (nextToken: string) => {
      setAuthToken(nextToken);
      setToken(nextToken);
      queryClient.clear();
    },
    [queryClient],
  );

  if (!token) {
    return <LoginScreen onSignedIn={onSignedIn} />;
  }

  if (session.isPending) {
    return <LoadingScreen />;
  }

  if (session.isError || !contextValue) {
    return <LoginScreen error={loginErrorMessage(session.error)} onSignedIn={onSignedIn} />;
  }

  return <SessionContext.Provider value={contextValue}>{children}</SessionContext.Provider>;
}

function isPublicMarketingPath(pathname: string): boolean {
  const normalized = pathname === "/" ? pathname : pathname.replace(/\/+$/, "");
  return PUBLIC_MARKETING_PATHS.has(normalized);
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

function signInErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "That username and password do not match an account.";
  }
  if (error instanceof ApiError && error.status === 429) {
    return "Too many attempts. Wait a moment before trying again.";
  }
  if (error instanceof ApiError) {
    return `Sign-in failed (${error.status} ${error.statusText}).`;
  }
  return "Sign-in failed. Check that the control plane is running, then retry.";
}

function LoginScreen({
  error,
  onSignedIn,
}: {
  error?: string;
  onSignedIn: (token: string) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const signIn = useMutation({
    ...signInMutationOptions(),
    onSuccess: (session) => onSignedIn(session.token),
  });
  // Arriving on the device-approval URL while signed out: after signing in the
  // route renders in place, so say why this is showing first.
  const approvingDevice = typeof window !== "undefined" && window.location.pathname === "/activate";
  const failure = error ?? (signIn.error ? signInErrorMessage(signIn.error) : undefined);

  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <section className="panel w-full max-w-md rounded-md p-5">
        <div className="mb-5">
          <div className="flex items-center gap-2">
            <img src="/lazycloud.png" alt="" className="size-8" />
            <span className="text-xl font-bold text-brand">LazyCloud</span>
          </div>
          <h1 className="mt-3 text-xl font-semibold">Sign in</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Use your account to reach the control plane. New installations create the first
            administrator offline.
          </p>
        </div>

        {approvingDevice ? (
          <div className="mb-3 rounded border border-border bg-muted/40 p-2 text-sm text-muted-foreground">
            A CLI is waiting for approval. Sign in to continue to the device-approval step.
          </div>
        ) : null}

        {failure ? (
          <div className="mb-3 rounded border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">
            {failure}
          </div>
        ) : null}

        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (username.trim() && password) {
              signIn.mutate({ username: username.trim(), password });
            }
          }}
        >
          <label className="block text-xs font-medium text-muted-foreground">
            Username
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              className="mono mt-1 h-9 w-full rounded-md border border-input bg-muted px-3 text-sm text-foreground outline-none focus:border-ring"
              type="text"
              autoComplete="username"
              autoFocus
            />
          </label>
          <label className="block text-xs font-medium text-muted-foreground">
            Password
            <input
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="mono mt-1 h-9 w-full rounded-md border border-input bg-muted px-3 text-sm text-foreground outline-none focus:border-ring"
              type="password"
              autoComplete="current-password"
            />
          </label>
          <Button
            type="submit"
            className="w-full"
            disabled={signIn.isPending || !username.trim() || !password}
          >
            {signIn.isPending ? <Loader2 className="size-4 animate-spin" /> : "Sign in"}
          </Button>
        </form>
      </section>
    </main>
  );
}

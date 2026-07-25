import { useCallback, useMemo, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouterState } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CliHint } from "@/components/shared/CliHint";
import { ApiError, clearAuthToken, setAuthToken } from "@/lib/api/client";
import { getStoredAuthToken } from "@/lib/auth";
import { workspacesQueryOptions } from "@/lib/queries/workspace";
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
  const workspaces = useQuery({
    ...workspacesQueryOptions(),
    enabled: !!token,
  });

  const logout = useCallback(() => {
    clearAuthToken();
    setToken(null);
    queryClient.clear();
  }, [queryClient]);

  const contextValue = useMemo<SessionContextValue | null>(
    () =>
      workspaces.data?.length
        ? {
            workspaces: workspaces.data,
            logout,
          }
        : null,
    [logout, workspaces.data],
  );

  if (!token) {
    return (
      <LoginScreen
        onAuthenticated={(nextToken) => {
          setAuthToken(nextToken);
          setToken(nextToken);
        }}
      />
    );
  }

  if (workspaces.isPending) {
    return <LoadingScreen />;
  }

  if (workspaces.isError || !contextValue) {
    return (
      <LoginScreen
        error={loginErrorMessage(workspaces.error)}
        onAuthenticated={(nextToken) => {
          setAuthToken(nextToken);
          setToken(nextToken);
        }}
      />
    );
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
    return "Your session expired or the token was revoked. Enter a new access token.";
  }
  if (error instanceof ApiError) {
    return `The control plane rejected the request (${error.status} ${error.statusText}).`;
  }
  if (error instanceof Error && error.message) {
    return "The control plane is unreachable. Check that the API is running, then retry.";
  }
  return "Token could not be validated.";
}

function LoginScreen({
  error,
  onAuthenticated,
}: {
  error?: string;
  onAuthenticated: (token: string) => void;
}) {
  const [tokenValue, setTokenValue] = useState("");
  // Arriving on the device-approval URL while signed out: after sign-in the
  // route renders in place, so tell the user why they are seeing this first.
  const approvingDevice =
    typeof window !== "undefined" && window.location.pathname === "/activate";
  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <section className="panel w-full max-w-md rounded-md p-5">
        <div className="mb-5">
          <div className="flex items-center gap-2">
            <img src="/lazycloud.png" alt="" className="size-8" />
            <span className="text-xl font-bold text-brand">LazyCloud</span>
          </div>
          <h1 className="mt-3 text-xl font-semibold">Enter an access token</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Use a workspace or administrator token to inspect the control plane. New installations
            must create the first administrator credential offline.
          </p>
        </div>

        {approvingDevice ? (
          <div className="mb-3 rounded border border-border bg-muted/40 p-2 text-sm text-muted-foreground">
            A CLI is waiting for approval. Sign in to continue to the device-approval step.
          </div>
        ) : null}

        {error ? <div className="mb-3 rounded border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">{error}</div> : null}

        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (tokenValue.trim()) onAuthenticated(tokenValue.trim());
          }}
        >
          <label className="block text-xs font-medium text-muted-foreground">
            Token
            <input
              value={tokenValue}
              onChange={(event) => setTokenValue(event.target.value)}
              className="mono mt-1 h-9 w-full rounded-md border border-input bg-muted px-3 text-sm text-foreground outline-none focus:border-ring"
              type="password"
              autoComplete="current-password"
            />
          </label>
          <Button type="submit" className="w-full">Continue</Button>
          <CliHint command="lazycloud-admin auth bootstrap --output ./lazycloud-admin-token" />
        </form>
      </section>
    </main>
  );
}

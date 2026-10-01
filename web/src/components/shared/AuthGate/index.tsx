import { useCallback, useMemo, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { ContentTransition } from "@/components/shared/ContentTransition";
import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { Button } from "@/components/ui/button";
import { isApiError } from "@/lib/api/client";
import { meQueryOptions, signOut } from "@/lib/queries/auth";
import { SessionContext, type SessionContextValue } from "@/components/shared/AuthGate/session";
import { SignInScreen } from "@/components/shared/AuthGate/SignInScreen";

export function AuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  // One request answers both questions the shell needs: who is signed in, and which
  // workspaces they reach. Resolving them separately would let the two disagree.
  const me = useQuery(meQueryOptions());

  const logout = useCallback(() => {
    // The server ends the session the cookie names and clears the cookie. A
    // failure to reach it still leaves the dashboard; the session then ends at
    // expiry.
    void signOut()
      .catch(() => undefined)
      .finally(() => {
        queryClient.clear();
        // Out to the public landing page rather than the sign-in screen. Signing
        // out is leaving, and being handed the way back in is the one thing
        // somebody who just left did not ask for.
        void navigate({ to: "/" });
      });
  }, [navigate, queryClient]);

  const contextValue = useMemo<SessionContextValue | null>(
    () =>
      me.data
        ? {
            // An account that never signed in with GitHub has no name; the
            // shell always has something to call it by.
            user: {
              ...me.data.user,
              display_name:
                me.data.user.display_name || me.data.user.github_login || me.data.user.email,
            },
            workspaces: me.data.workspaces,
            logout,
          }
        : null,
    [logout, me.data],
  );

  if (me.isPending) {
    return <LoadingScreen />;
  }

  if (me.isError) {
    return (
      <PreShellScreen>
        <ApiErrorNotice
          title="Could not load your session"
          error={me.error}
          onRetry={() => void me.refetch()}
          retrying={me.isFetching}
        />
        {isApiError(me.error, 403) ? (
          <Button variant="outline" onClick={logout}>
            Sign out
          </Button>
        ) : null}
      </PreShellScreen>
    );
  }

  if (!contextValue) {
    return <SignInScreen />;
  }

  return <SessionContext.Provider value={contextValue}>{children}</SessionContext.Provider>;
}

function LoadingScreen() {
  return (
    <ContentTransition
      pending
      role="status"
      aria-label="Loading account"
      className="flex min-h-screen items-center justify-center bg-background"
    >
      <Loader2
        className="size-5 animate-spin text-muted-foreground motion-reduce:animate-none"
        aria-hidden="true"
      />
    </ContentTransition>
  );
}

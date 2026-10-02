import { createContext, useContext, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { Schemas } from "@/lib/api/client";
import { currentSessionQueryOptions } from "@/lib/queries/auth";

/** The signed-in account and the workspaces it reaches, as `GET /v1/me` answers. */
export type SessionContextValue = Schemas["Me"] & {
  logout: () => void;
};

export const SessionContext = createContext<SessionContextValue | null>(null);

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) {
    throw new Error("useSession must be used inside AuthGate");
  }
  return value;
}

/** What to call an account: an account made outside GitHub sign-in has no display name. */
export function accountName(user: Schemas["User"]): string {
  return user.display_name || user.github_login || user.email;
}

/**
 * Whether the session is being re-read for a workspace it does not list.
 *
 * The list can trail a workspace created, joined or renamed in another tab, so a
 * name it lacks reads the session once more before the page calls it not found.
 * Each name is re-read at most once.
 */
export function useSessionRecheck(workspaceName: string, missing: boolean): boolean {
  const queryClient = useQueryClient();
  const [checked, setChecked] = useState<string | null>(null);
  // A ref as well as state, so StrictMode's second effect run does not read twice.
  const requested = useRef<string | null>(null);
  const pending = missing && checked !== workspaceName;

  useEffect(() => {
    if (!pending || requested.current === workspaceName) return;
    requested.current = workspaceName;
    void queryClient
      .refetchQueries({ queryKey: currentSessionQueryOptions().queryKey, exact: true })
      .finally(() => setChecked(workspaceName));
  }, [pending, queryClient, workspaceName]);

  return pending;
}

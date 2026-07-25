import { createContext, useContext } from "react";

import type { Workspace } from "@/lib/api/schemas";

export type SessionContextValue = {
  workspaces: Workspace[];
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

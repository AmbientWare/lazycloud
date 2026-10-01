import { createContext, useContext } from "react";

import type { Schemas } from "@/lib/api/client";

export type SessionContextValue = {
  user: Schemas["User"];
  workspaces: Schemas["Workspace"][];
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

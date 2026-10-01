import { createContext, useContext } from "react";

import type { Schemas } from "@/lib/api/client";

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

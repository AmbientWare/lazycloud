import { createContext, useContext } from "react";

import type { Schemas } from "@/lib/api/client";

export type WorkspaceContextValue = {
  workspace: Schemas["Workspace"];
  workspaces: Schemas["Workspace"][];
};

export const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

/** Active workspace resolved from the `/w/$workspace` route param. */
export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext);
  if (!value) {
    throw new Error("useWorkspace must be used inside the /w/$workspace layout");
  }
  return value;
}

import { createContext, useContext } from "react";

import type { Schemas } from "@/lib/api/client";
import type { EventStreamStatus } from "@/hooks/useEventStream";

export type WorkspaceContextValue = {
  workspace: Schemas["Workspace"];
  workspaces: Schemas["Workspace"][];
};

export const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export type WorkspaceLiveUpdatesContextValue = {
  status: EventStreamStatus;
};

export const WorkspaceLiveUpdatesContext = createContext<WorkspaceLiveUpdatesContextValue | null>(
  null,
);

/** Active workspace resolved from the `/w/$workspace` route param. */
export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext);
  if (!value) {
    throw new Error("useWorkspace must be used inside the /w/$workspace layout");
  }
  return value;
}

export function useWorkspaceLiveUpdates(): WorkspaceLiveUpdatesContextValue {
  const value = useContext(WorkspaceLiveUpdatesContext);
  if (!value) throw new Error("useWorkspaceLiveUpdates must be used inside a workspace layout");
  return value;
}

import { createContext, useContext } from "react";

import type { WorkspaceDeletionController } from "./controller";

export const WorkspaceDeletionContext =
  createContext<WorkspaceDeletionController | null>(null);

export function useWorkspaceDeletion(): WorkspaceDeletionController {
  const value = useContext(WorkspaceDeletionContext);
  if (!value) {
    throw new Error(
      "useWorkspaceDeletion must be used inside WorkspaceDeletionProvider",
    );
  }
  return value;
}

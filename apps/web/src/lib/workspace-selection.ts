import { create } from "zustand";
import { persist } from "zustand/middleware";

type WorkspaceSelectionState = {
  lastWorkspaceName: string | null;
  rememberWorkspaceName: (workspaceName: string) => void;
};

export const useWorkspaceSelection = create<WorkspaceSelectionState>()(
  persist(
    (set) => ({
      lastWorkspaceName: null,
      rememberWorkspaceName: (workspaceName) => {
        set({ lastWorkspaceName: workspaceName });
      },
    }),
    {
      name: "lazycloud.selected-workspace",
    },
  ),
);

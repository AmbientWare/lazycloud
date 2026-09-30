import { create } from "zustand";
import { persist } from "zustand/middleware";

type WorkspaceSelectionState = {
  lastWorkspaceName: string | null;
  rememberWorkspaceName: (workspaceName: string) => void;
  /**
   * Stop returning to a workspace this account no longer reaches.
   *
   * The remembered name outlives the membership that made it reachable, and the
   * dashboard sends you back to it on the next visit, where every request is
   * refused. Only clears the name it is given, so leaving one workspace does not
   * forget a different one somebody switched to meanwhile.
   */
  forgetWorkspaceName: (workspaceName: string) => void;
};

export const useWorkspaceSelection = create<WorkspaceSelectionState>()(
  persist(
    (set) => ({
      lastWorkspaceName: null,
      rememberWorkspaceName: (workspaceName) => {
        set({ lastWorkspaceName: workspaceName });
      },
      forgetWorkspaceName: (workspaceName) => {
        set((state) =>
          state.lastWorkspaceName === workspaceName ? { lastWorkspaceName: null } : state,
        );
      },
    }),
    {
      name: "lazycloud.selected-workspace",
    },
  ),
);

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import type { Schemas } from "@/lib/api/client";
import { meQueryKey } from "@/lib/queries/auth";
import { deleteWorkspace } from "@/lib/queries/workspace";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

type Workspace = Schemas["Workspace"];
type Me = Schemas["Me"];

export type WorkspaceDeletionTarget = {
  workspace: Workspace;
  selected: boolean;
};

type WorkspaceDeletionControllerOptions = {
  canManage: boolean;
  lastWorkspaceName: string | null;
  rememberWorkspaceName: (workspaceName: string) => void;
  replacePath: (path: string) => void;
  workspaces: Workspace[];
  deleteCommand?: (workspace: string) => Promise<Workspace>;
};

export function useWorkspaceDeletionController({
  canManage,
  lastWorkspaceName,
  rememberWorkspaceName,
  replacePath,
  workspaces,
  deleteCommand = deleteWorkspace,
}: WorkspaceDeletionControllerOptions) {
  const queryClient = useQueryClient();
  const commandInFlight = useRef(false);
  const [target, setTarget] = useState<WorkspaceDeletionTarget | null>(null);
  const [confirmation, setConfirmation] = useState("");

  const remove = useMutation({
    mutationFn: (requested: WorkspaceDeletionTarget) => deleteCommand(requested.workspace.name),
    onSuccess: async (deleting, requested) => {
      const rootKey = workspaceQueryKeys.root(requested.workspace.name);

      await queryClient.cancelQueries({ queryKey: rootKey });
      queryClient.removeQueries({ queryKey: rootKey });

      // The session owns the account's workspace list. The workspace stays in it
      // as deleting until its cleanup finishes, so it has to change state there
      // before anything navigates.
      const session = queryClient.getQueryData<Me>(meQueryKey);
      const listed = (session?.workspaces ?? workspaces).map((item) =>
        item.id === deleting.id ? { ...item, state: deleting.state } : item,
      );
      if (session) queryClient.setQueryData<Me>(meQueryKey, { ...session, workspaces: listed });
      const remaining = listed.filter((item) => item.id !== deleting.id);

      const nextWorkspace = remaining.find((item) => item.state === "active") ?? remaining[0];
      if (nextWorkspace && (requested.selected || lastWorkspaceName === requested.workspace.name)) {
        rememberWorkspaceName(nextWorkspace.name);
      }
      if (requested.selected && nextWorkspace) {
        replacePath(`/w/${encodeURIComponent(nextWorkspace.name)}/apps`);
      }

      setTarget(null);
      setConfirmation("");
      void queryClient.invalidateQueries({ queryKey: meQueryKey, exact: true });
    },
    onSettled: () => {
      commandInFlight.current = false;
    },
  });

  const availability = (workspace: Workspace) => {
    if (!canManage) {
      return {
        allowed: false as const,
        reason: "Administrator access is required",
      };
    }
    if (workspace.state === "deleting") {
      return { allowed: true as const };
    }
    // A hint, not the decision: the server refuses deleting the last active
    // workspace regardless.
    if (workspaces.filter((item) => item.state === "active").length <= 1) {
      return { allowed: false as const, reason: "The final workspace is protected" };
    }
    return { allowed: true as const };
  };

  const begin = (workspace: Workspace, selected: boolean) => {
    if (!availability(workspace).allowed) return;
    remove.reset();
    setConfirmation("");
    setTarget({ workspace, selected });
  };

  const close = () => {
    if (remove.isPending) return;
    remove.reset();
    setConfirmation("");
    setTarget(null);
  };

  const submit = () => {
    if (
      !target ||
      confirmation !== target.workspace.name ||
      remove.isPending ||
      commandInFlight.current
    ) {
      return;
    }
    commandInFlight.current = true;
    remove.mutate(target);
  };

  return {
    availability,
    begin,
    canManage,
    close,
    confirmation,
    error: remove.error,
    isPending: remove.isPending,
    setConfirmation,
    submit,
    target,
  };
}

export type WorkspaceDeletionController = ReturnType<typeof useWorkspaceDeletionController>;

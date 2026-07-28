import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import type { Workspace } from "@/lib/api/schemas";
import { deleteWorkspace, workspacesQueryOptions } from "@/lib/queries/workspace";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { workspaceDeleteAvailability } from "@/lib/workspace-deletion";

export type WorkspaceDeletionTarget = {
  workspace: Workspace;
  selected: boolean;
};

type WorkspaceDeletionControllerOptions = {
  canManage: boolean;
  currentWorkspaceId: string | undefined;
  lastWorkspaceName: string | null;
  rememberWorkspaceName: (workspaceName: string) => void;
  replacePath: (path: string) => void;
  workspaces: Workspace[];
  deleteCommand?: (workspaceId: string) => Promise<null>;
};

export function useWorkspaceDeletionController({
  canManage,
  currentWorkspaceId,
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
    mutationFn: (requested: WorkspaceDeletionTarget) => deleteCommand(requested.workspace.id),
    onSuccess: async (_result, requested) => {
      const workspaceId = requested.workspace.id;
      const rootKey = workspaceQueryKeys.root(workspaceId);
      const directoryKey = workspacesQueryOptions().queryKey;

      await queryClient.cancelQueries({ queryKey: rootKey });
      queryClient.removeQueries({ queryKey: rootKey });

      const latestDirectory = queryClient.getQueryData<Workspace[]>(directoryKey) ?? workspaces;
      const remaining = latestDirectory.filter((item) => item.id !== workspaceId);
      queryClient.setQueryData(directoryKey, remaining);

      const nextWorkspace = remaining.find((item) => item.status === "active") ?? remaining[0];
      if (nextWorkspace && (requested.selected || lastWorkspaceName === requested.workspace.name)) {
        rememberWorkspaceName(nextWorkspace.name);
      }
      if (requested.selected && nextWorkspace) {
        replacePath(`/w/${encodeURIComponent(nextWorkspace.name)}/apps`);
      }

      setTarget(null);
      setConfirmation("");
      void queryClient.invalidateQueries({
        queryKey: directoryKey,
        exact: true,
      });
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
    if (workspace.status === "deleting") {
      return { allowed: true as const };
    }
    const activeWorkspaceCount = workspaces.filter((item) => item.status === "active").length;
    return workspaceDeleteAvailability(workspace, currentWorkspaceId, activeWorkspaceCount);
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

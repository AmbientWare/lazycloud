import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiProtocolError } from "@/lib/api/client";
import type { CurrentSession, Workspace } from "@/lib/api/schemas";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import { currentWorkspaceQueryOptions, updateWorkspace } from "@/lib/queries/workspace";

export type WorkspaceIdentityMode = "idle" | "editing" | "saving" | "saved" | "error";

type WorkspaceIdentityState = {
  workspaceId: string;
  sourceName: string;
  draftName: string;
  mode: WorkspaceIdentityMode;
  error: Error | null;
};

type RenameCommand = {
  workspaceId: string;
  name: string;
};

export type WorkspaceIdentityController = {
  mode: WorkspaceIdentityMode;
  draftName: string;
  error: Error | null;
  isEditing: boolean;
  isSaving: boolean;
  canSave: boolean;
  beginEditing: () => void;
  setDraftName: (name: string) => void;
  cancel: () => void;
  save: () => void;
};

export function useWorkspaceIdentityController({
  workspace,
  replacePath,
}: {
  workspace: Workspace;
  replacePath: (path: string) => void;
}): WorkspaceIdentityController {
  const queryClient = useQueryClient();
  const [state, setState] = useState<WorkspaceIdentityState>(() => idleState(workspace));
  const activeSaves = useRef(new Set<string>());

  const workspaceChanged =
    state.workspaceId !== workspace.id || state.sourceName !== workspace.name;
  const currentState = workspaceChanged ? idleState(workspace) : state;
  if (workspaceChanged) setState(currentState);

  const rename = useMutation({
    mutationFn: async (command: RenameCommand) => {
      const updated = await updateWorkspace(command.workspaceId, command.name);
      if (updated.id !== command.workspaceId) {
        throw new ApiProtocolError("The API returned a different workspace after rename");
      }
      return updated;
    },
    onSuccess: (updated, command) => {
      // The session is what the shell and this dialog read, so the rename has to land
      // there; a second list nothing observes would just go stale.
      queryClient.setQueryData<CurrentSession>(currentSessionQueryOptions().queryKey, (session) =>
        session
          ? {
              ...session,
              workspaces: session.workspaces.map((item) =>
                item.id === updated.id ? updated : item,
              ),
            }
          : session,
      );
      queryClient.setQueryData<Workspace>(currentWorkspaceQueryOptions().queryKey, (owner) =>
        owner?.id === updated.id ? updated : owner,
      );
      setState((current) => {
        if (current.workspaceId !== command.workspaceId) return current;
        return {
          ...current,
          draftName: updated.name,
          mode: "saved",
          error: null,
        };
      });
      if (workspace.id === command.workspaceId) {
        replacePath(`/w/${encodeURIComponent(updated.name)}/settings`);
      }
    },
    onError: (error, command) => {
      setState((current) => {
        if (current.workspaceId !== command.workspaceId) return current;
        return {
          ...current,
          mode: "error",
          error: asError(error),
        };
      });
    },
    onSettled: (_updated, _error, command) => {
      activeSaves.current.delete(command.workspaceId);
    },
  });

  const normalizedName = normalizeWorkspaceName(currentState.draftName);
  const editing = isEditorMode(currentState.mode);
  const saving = currentState.mode === "saving";
  const canSave =
    editing && !saving && validWorkspaceName(normalizedName) && normalizedName !== workspace.name;

  return {
    mode: currentState.mode,
    draftName: currentState.draftName,
    error: currentState.error,
    isEditing: editing,
    isSaving: saving,
    canSave,
    beginEditing: () => {
      if (activeSaves.current.has(workspace.id)) return;
      const editingState: WorkspaceIdentityState = {
        workspaceId: workspace.id,
        sourceName: workspace.name,
        draftName: workspace.name,
        mode: "editing",
        error: null,
      };
      setState(editingState);
    },
    setDraftName: (draftName) => {
      const current = currentState;
      if (!isEditorMode(current.mode) || current.mode === "saving") return;
      setState({
        ...current,
        draftName,
        mode: "editing",
        error: null,
      });
    },
    cancel: () => {
      if (currentState.mode === "saving" || activeSaves.current.has(workspace.id)) return;
      setState(idleState(workspace));
    },
    save: () => {
      const current = currentState;
      const name = normalizeWorkspaceName(current.draftName);
      if (
        !isEditorMode(current.mode) ||
        current.mode === "saving" ||
        !validWorkspaceName(name) ||
        name === workspace.name ||
        activeSaves.current.has(workspace.id)
      ) {
        return;
      }
      activeSaves.current.add(workspace.id);
      const savingState: WorkspaceIdentityState = {
        ...current,
        draftName: name,
        mode: "saving",
        error: null,
      };
      setState(savingState);
      rename.mutate({ workspaceId: workspace.id, name });
    },
  };
}

function idleState(workspace: Workspace): WorkspaceIdentityState {
  return {
    workspaceId: workspace.id,
    sourceName: workspace.name,
    draftName: workspace.name,
    mode: "idle",
    error: null,
  };
}

function isEditorMode(mode: WorkspaceIdentityMode): boolean {
  return mode === "editing" || mode === "saving" || mode === "error";
}

function normalizeWorkspaceName(name: string): string {
  return name.trim();
}

function validWorkspaceName(name: string): boolean {
  return /^[a-z][a-z0-9_-]{0,62}$/.test(name);
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error("Workspace could not be renamed");
}

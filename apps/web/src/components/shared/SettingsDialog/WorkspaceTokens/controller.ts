import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import type { AuthToken, TokenListResponse } from "@/lib/api/schemas";
import {
  createToken,
  deleteToken,
  toggleToken,
  tokensQueryOptions,
  type CreateTokenInput,
} from "@/lib/queries/tokens";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

type CreateMode = "closed" | "drafting" | "creating" | "issued" | "error";
type TokenActionMode = "idle" | "confirming" | "toggling" | "deleting" | "error";
type TokenActionKind = "toggle" | "delete";

type CreateState = {
  mode: CreateMode;
  secret: string | null;
  error: Error | null;
};

type TokenActionState = {
  mode: TokenActionMode;
  kind: TokenActionKind | null;
  tokenId: string | null;
  error: Error | null;
};

type ControllerState = {
  workspaceId: string;
  create: CreateState;
  action: TokenActionState;
};

type ActiveCommand = {
  id: number;
  workspaceId: string;
  kind: "create" | TokenActionKind;
  tokenId: string | null;
};

export type WorkspaceTokensController = {
  tokens: readonly AuthToken[];
  isLoading: boolean;
  loadError: Error | null;
  createMode: CreateMode;
  createError: Error | null;
  issuedSecret: string | null;
  beginCreate: () => void;
  cancelCreate: () => void;
  create: (input: CreateTokenInput) => void;
  acknowledgeIssued: () => void;
  actionMode: TokenActionMode;
  actionKind: TokenActionKind | null;
  actionError: Error | null;
  actionTokenId: string | null;
  beginDelete: (token: AuthToken) => void;
  cancelDelete: () => void;
  toggle: (token: AuthToken) => void;
  deleteConfirmed: (token: AuthToken) => void;
  isCommandPending: boolean;
};

export function isManagedToken(token: AuthToken): boolean {
  return token.kind !== "workspace";
}

export function useWorkspaceTokensController(workspaceId: string): WorkspaceTokensController {
  const queryClient = useQueryClient();
  const query = useQuery(tokensQueryOptions(workspaceId));
  const activeCommand = useRef<ActiveCommand | null>(null);
  const nextCommandId = useRef(0);
  const [state, setState] = useState<ControllerState>(() => initialState(workspaceId));

  const ownedState = state.workspaceId === workspaceId ? state : initialState(workspaceId);
  if (state.workspaceId !== workspaceId) {
    setState(ownedState);
  }

  useEffect(() => {
    if (activeCommand.current?.workspaceId !== workspaceId) {
      activeCommand.current = null;
    }
  }, [workspaceId]);

  const commandIsCurrent = (command: ActiveCommand) => activeCommand.current?.id === command.id;

  const finishCommand = (command: ActiveCommand) => {
    if (activeCommand.current?.id === command.id) {
      activeCommand.current = null;
    }
  };

  const startCommand = (
    kind: ActiveCommand["kind"],
    tokenId: string | null,
  ): ActiveCommand | null => {
    if (activeCommand.current !== null) return null;
    const command = {
      id: ++nextCommandId.current,
      workspaceId,
      kind,
      tokenId,
    };
    activeCommand.current = command;
    return command;
  };

  const beginCreate = () => {
    if (
      activeCommand.current !== null ||
      ownedState.create.mode !== "closed" ||
      ownedState.action.mode !== "idle"
    ) {
      return;
    }
    setState((current) => ({
      ...owned(current, workspaceId),
      create: { mode: "drafting", secret: null, error: null },
    }));
  };

  const cancelCreate = () => {
    if (activeCommand.current !== null || !["drafting", "error"].includes(ownedState.create.mode)) {
      return;
    }
    setState((current) => ({
      ...owned(current, workspaceId),
      create: closedCreateState(),
    }));
  };

  const runCreate = (input: CreateTokenInput) => {
    if (!["drafting", "error"].includes(ownedState.create.mode)) return;
    const command = startCommand("create", null);
    if (!command) return;
    setState((current) => ({
      ...owned(current, workspaceId),
      create: { mode: "creating", secret: null, error: null },
    }));
    void createToken(command.workspaceId, input)
      .then((result) => {
        insertTokenRecord(queryClient, command.workspaceId, result.record);
        if (!commandIsCurrent(command)) return;
        setState((current) => ({
          ...owned(current, command.workspaceId),
          create: { mode: "issued", secret: result.token, error: null },
        }));
      })
      .catch((error: unknown) => {
        if (!commandIsCurrent(command)) return;
        setState((current) => ({
          ...owned(current, command.workspaceId),
          create: { mode: "error", secret: null, error: asError(error) },
        }));
      })
      .finally(() => finishCommand(command));
  };

  const acknowledgeIssued = () => {
    if (ownedState.create.mode !== "issued") return;
    setState((current) => ({
      ...owned(current, workspaceId),
      create: closedCreateState(),
    }));
  };

  const beginDelete = (token: AuthToken) => {
    if (
      !tokenCanBeManaged(token, workspaceId) ||
      activeCommand.current !== null ||
      ownedState.create.mode !== "closed"
    ) {
      return;
    }
    setState((current) => ({
      ...owned(current, workspaceId),
      action: {
        mode: "confirming",
        kind: "delete",
        tokenId: token.id,
        error: null,
      },
    }));
  };

  const cancelDelete = () => {
    if (
      activeCommand.current !== null ||
      ownedState.action.kind !== "delete" ||
      !["confirming", "error"].includes(ownedState.action.mode)
    ) {
      return;
    }
    setState((current) => ({
      ...owned(current, workspaceId),
      action: idleActionState(),
    }));
  };

  const runToggle = (token: AuthToken) => {
    if (!tokenCanBeManaged(token, workspaceId) || ownedState.create.mode !== "closed") {
      return;
    }
    const command = startCommand("toggle", token.id);
    if (!command) return;
    setState((current) => ({
      ...owned(current, workspaceId),
      action: {
        mode: "toggling",
        kind: "toggle",
        tokenId: token.id,
        error: null,
      },
    }));
    void toggleToken(command.workspaceId, token.id)
      .then((record) => {
        replaceTokenRecord(queryClient, command.workspaceId, record);
        if (!commandIsCurrent(command)) return;
        setState((current) => ({
          ...owned(current, command.workspaceId),
          action: idleActionState(),
        }));
      })
      .catch((error: unknown) => {
        if (!commandIsCurrent(command)) return;
        setState((current) => ({
          ...owned(current, command.workspaceId),
          action: {
            mode: "error",
            kind: "toggle",
            tokenId: token.id,
            error: asError(error),
          },
        }));
      })
      .finally(() => finishCommand(command));
  };

  const runDelete = (token: AuthToken) => {
    if (
      !tokenCanBeManaged(token, workspaceId) ||
      ownedState.action.kind !== "delete" ||
      ownedState.action.tokenId !== token.id ||
      !["confirming", "error"].includes(ownedState.action.mode)
    ) {
      return;
    }
    const command = startCommand("delete", token.id);
    if (!command) return;
    setState((current) => ({
      ...owned(current, workspaceId),
      action: {
        mode: "deleting",
        kind: "delete",
        tokenId: token.id,
        error: null,
      },
    }));
    void deleteToken(command.workspaceId, token.id)
      .then(() => {
        removeTokenRecord(queryClient, command.workspaceId, token.id);
        if (!commandIsCurrent(command)) return;
        setState((current) => ({
          ...owned(current, command.workspaceId),
          action: idleActionState(),
        }));
      })
      .catch((error: unknown) => {
        if (!commandIsCurrent(command)) return;
        setState((current) => ({
          ...owned(current, command.workspaceId),
          action: {
            mode: "error",
            kind: "delete",
            tokenId: token.id,
            error: asError(error),
          },
        }));
      })
      .finally(() => finishCommand(command));
  };

  return {
    tokens: query.data?.tokens ?? [],
    isLoading: query.isPending,
    loadError: query.error,
    createMode: ownedState.create.mode,
    createError: ownedState.create.error,
    issuedSecret: ownedState.create.secret,
    beginCreate,
    cancelCreate,
    create: runCreate,
    acknowledgeIssued,
    actionMode: ownedState.action.mode,
    actionKind: ownedState.action.kind,
    actionError: ownedState.action.error,
    actionTokenId: ownedState.action.tokenId,
    beginDelete,
    cancelDelete,
    toggle: runToggle,
    deleteConfirmed: runDelete,
    isCommandPending:
      ownedState.create.mode === "creating" ||
      ownedState.action.mode === "toggling" ||
      ownedState.action.mode === "deleting",
  };
}

function initialState(workspaceId: string): ControllerState {
  return {
    workspaceId,
    create: closedCreateState(),
    action: idleActionState(),
  };
}

function owned(state: ControllerState, workspaceId: string): ControllerState {
  return state.workspaceId === workspaceId ? state : initialState(workspaceId);
}

function closedCreateState(): CreateState {
  return { mode: "closed", secret: null, error: null };
}

function idleActionState(): TokenActionState {
  return { mode: "idle", kind: null, tokenId: null, error: null };
}

function tokenCanBeManaged(token: AuthToken, workspaceId: string): boolean {
  return token.workspace_id === workspaceId && !isManagedToken(token);
}

function insertTokenRecord(queryClient: QueryClient, workspaceId: string, record: AuthToken): void {
  queryClient.setQueryData<TokenListResponse>(
    workspaceQueryKeys.settings.tokens(workspaceId),
    (current) => ({
      tokens: [...(current?.tokens.filter((token) => token.id !== record.id) ?? []), record],
    }),
  );
}

function replaceTokenRecord(
  queryClient: QueryClient,
  workspaceId: string,
  record: AuthToken,
): void {
  queryClient.setQueryData<TokenListResponse>(
    workspaceQueryKeys.settings.tokens(workspaceId),
    (current) => ({
      tokens: current
        ? current.tokens.map((token) => (token.id === record.id ? record : token))
        : [record],
    }),
  );
}

function removeTokenRecord(queryClient: QueryClient, workspaceId: string, tokenId: string): void {
  queryClient.setQueryData<TokenListResponse>(
    workspaceQueryKeys.settings.tokens(workspaceId),
    (current) => ({
      tokens: current?.tokens.filter((token) => token.id !== tokenId) ?? [],
    }),
  );
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

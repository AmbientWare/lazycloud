import { useRef, useState } from "react";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import type { AuthToken, TokenListResponse } from "@/lib/api/schemas";
import {
  createToken,
  deleteToken,
  revokeToken,
  tokensQueryOptions,
  type CreateTokenInput,
} from "@/lib/queries/tokens";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

type CreateMode = "closed" | "drafting" | "creating" | "issued" | "error";
export type TokenActionKind = "revoke" | "delete";
type ActionMode = "idle" | "confirming" | "running" | "error";

/** A secret and just enough of its record to name what was created. */
export type IssuedToken = {
  secret: string;
  name: string;
  prefix: string;
};

// Unions rather than one wide record: the secret exists in exactly one state, so
// there is no shape where a stale secret can survive alongside another mode.
type CreateState =
  | { mode: "closed" | "drafting" | "creating" }
  | { mode: "issued"; issued: IssuedToken }
  | { mode: "error"; error: Error };

type ActionState =
  | { mode: "idle" }
  | { mode: "confirming" | "running"; kind: TokenActionKind; tokenId: string }
  | { mode: "error"; kind: TokenActionKind; tokenId: string; error: Error };

type ControllerState = {
  create: CreateState;
  action: ActionState;
};

export type AccessTokensController = {
  tokens: readonly AuthToken[];
  isLoading: boolean;
  loadError: Error | null;
  createMode: CreateMode;
  createError: Error | null;
  issued: IssuedToken | null;
  beginCreate: () => void;
  cancelCreate: () => void;
  create: (input: CreateTokenInput) => void;
  dismissIssued: () => void;
  actionMode: ActionMode;
  actionKind: TokenActionKind | null;
  actionTokenId: string | null;
  actionError: Error | null;
  beginAction: (kind: TokenActionKind, token: AuthToken) => void;
  cancelAction: () => void;
  confirmAction: () => void;
  isCommandPending: boolean;
};

const IDLE: ControllerState = {
  create: { mode: "closed" },
  action: { mode: "idle" },
};

export function useAccessTokensController(): AccessTokensController {
  const queryClient = useQueryClient();
  const query = useQuery(tokensQueryOptions());
  const [state, setState] = useState<ControllerState>(IDLE);
  // One command at a time, so a double submit cannot mint two credentials and a
  // confirm cannot race the request it is confirming.
  const commandRunning = useRef(false);

  const beginCreate = () => {
    if (commandRunning.current || state.create.mode !== "closed" || state.action.mode !== "idle") {
      return;
    }
    setState({ create: { mode: "drafting" }, action: { mode: "idle" } });
  };

  const cancelCreate = () => {
    if (commandRunning.current || !isDraftable(state.create.mode)) return;
    setState(IDLE);
  };

  const runCreate = (input: CreateTokenInput) => {
    if (commandRunning.current || !isDraftable(state.create.mode)) return;
    commandRunning.current = true;
    setState({ create: { mode: "creating" }, action: { mode: "idle" } });
    void createToken(input)
      .then((result) => {
        insertTokenRecord(queryClient, result.record);
        setState({
          create: {
            mode: "issued",
            issued: {
              secret: result.token,
              name: result.record.name,
              prefix: result.record.prefix,
            },
          },
          action: { mode: "idle" },
        });
      })
      .catch((error: unknown) => {
        setState({ create: { mode: "error", error: asError(error) }, action: { mode: "idle" } });
      })
      .finally(() => {
        commandRunning.current = false;
      });
  };

  /** Drops the only copy of the secret the browser holds. */
  const dismissIssued = () => {
    if (state.create.mode !== "issued") return;
    setState(IDLE);
  };

  const beginAction = (kind: TokenActionKind, token: AuthToken) => {
    if (commandRunning.current || state.create.mode !== "closed") return;
    setState({
      create: { mode: "closed" },
      action: { mode: "confirming", kind, tokenId: token.id },
    });
  };

  const cancelAction = () => {
    if (commandRunning.current || state.action.mode === "running") return;
    setState(IDLE);
  };

  const confirmAction = () => {
    const action = state.action;
    if (commandRunning.current) return;
    if (action.mode !== "confirming" && action.mode !== "error") return;
    const { kind, tokenId } = action;
    commandRunning.current = true;
    setState({ create: { mode: "closed" }, action: { mode: "running", kind, tokenId } });
    const request =
      kind === "revoke"
        ? revokeToken(tokenId).then((record) => replaceTokenRecord(queryClient, record))
        : deleteToken(tokenId).then(() => removeTokenRecord(queryClient, tokenId));
    void request
      .then(() => setState(IDLE))
      .catch((error: unknown) => {
        setState({
          create: { mode: "closed" },
          action: { mode: "error", kind, tokenId, error: asError(error) },
        });
      })
      .finally(() => {
        commandRunning.current = false;
      });
  };

  return {
    tokens: query.data?.tokens ?? [],
    isLoading: query.isPending,
    loadError: query.error,
    createMode: state.create.mode,
    createError: state.create.mode === "error" ? state.create.error : null,
    issued: state.create.mode === "issued" ? state.create.issued : null,
    beginCreate,
    cancelCreate,
    create: runCreate,
    dismissIssued,
    actionMode: state.action.mode,
    actionKind: state.action.mode === "idle" ? null : state.action.kind,
    actionTokenId: state.action.mode === "idle" ? null : state.action.tokenId,
    actionError: state.action.mode === "error" ? state.action.error : null,
    beginAction,
    cancelAction,
    confirmAction,
    isCommandPending: state.create.mode === "creating" || state.action.mode === "running",
  };
}

function isDraftable(mode: CreateMode): boolean {
  return mode === "drafting" || mode === "error";
}

function insertTokenRecord(queryClient: QueryClient, record: AuthToken): void {
  queryClient.setQueryData<TokenListResponse>(accountQueryKeys.tokens(), (current) => ({
    tokens: [...(current?.tokens.filter((token) => token.id !== record.id) ?? []), record],
  }));
}

function replaceTokenRecord(queryClient: QueryClient, record: AuthToken): void {
  queryClient.setQueryData<TokenListResponse>(accountQueryKeys.tokens(), (current) => ({
    tokens: current
      ? current.tokens.map((token) => (token.id === record.id ? record : token))
      : [record],
  }));
}

function removeTokenRecord(queryClient: QueryClient, tokenId: string): void {
  queryClient.setQueryData<TokenListResponse>(accountQueryKeys.tokens(), (current) => ({
    tokens: current?.tokens.filter((token) => token.id !== tokenId) ?? [],
  }));
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

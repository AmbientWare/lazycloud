import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import type { AwsConnection } from "@/lib/api/schemas";
import {
  cancelAwsConnectionReconnect,
  computeQueryKeys,
  createAwsConnection,
  reconnectAwsConnection,
  removeAwsConnection,
  retryAwsConnection,
  validateAwsConnection,
} from "@/lib/queries/compute";

import type { AwsConnectionDialogRecoveryAction } from "./lifecycle";

export type AwsConnectionDialogAction = "create" | AwsConnectionDialogRecoveryAction | "remove";

type AuthorizationCommand = {
  authorizationPopup: AwsAuthorizationPopup;
} & ({ action: "create"; accountId: string } | { action: "reconnect" });

type AwsConnectionCommand =
  AuthorizationCommand | { action: "validate" | "cancel_reconnect" | "retry" | "remove" };

type AwsConnectionMutationOutcome = {
  action: AwsConnectionDialogAction;
  connection: AwsConnection | null;
  authorizationPopup: AwsAuthorizationPopup | null;
  authorizationUrl: string | null;
};

export type AwsAuthorizationPopup = {
  close: () => void;
  handoff: (authorizationUrl: string) => void;
};

export type AwsConnectionController = {
  activeAction: AwsConnectionDialogAction | null;
  createError: Error | null;
  recoveryError: Error | null;
  removalError: Error | null;
  removeOpen: boolean;
  setRemoveOpen: (open: boolean) => void;
  create: (accountId: string) => void;
  validate: () => void;
  reconnect: () => void;
  cancelReconnect: () => void;
  retry: () => void;
  remove: () => void;
};

export function useAwsConnectionController({
  workspaceId,
  onClose,
  openAuthorizationPopup = openAwsAuthorizationPopup,
}: {
  workspaceId: string;
  onClose: () => void;
  openAuthorizationPopup?: () => AwsAuthorizationPopup;
}): AwsConnectionController {
  const queryClient = useQueryClient();
  const activeActionRef = useRef<AwsConnectionDialogAction | null>(null);
  const [activeAction, setActiveAction] = useState<AwsConnectionDialogAction | null>(null);
  const [lastAction, setLastAction] = useState<AwsConnectionDialogAction | null>(null);
  const [removeOpen, setRemoveOpen] = useState(false);

  const mutation = useMutation({
    mutationFn: async (command: AwsConnectionCommand): Promise<AwsConnectionMutationOutcome> => {
      switch (command.action) {
        case "create": {
          const result = await createAwsConnection(workspaceId, {
            accountId: command.accountId,
          });
          return {
            action: command.action,
            connection: result.connection,
            authorizationPopup: command.authorizationPopup,
            authorizationUrl: result.authorization.url,
          };
        }
        case "validate":
          return connectionOutcome(command.action, await validateAwsConnection(workspaceId));
        case "reconnect": {
          const result = await reconnectAwsConnection(workspaceId);
          return {
            action: command.action,
            connection: result.connection,
            authorizationPopup: command.authorizationPopup,
            authorizationUrl: result.authorization.url,
          };
        }
        case "cancel_reconnect":
          return connectionOutcome(command.action, await cancelAwsConnectionReconnect(workspaceId));
        case "retry":
          return connectionOutcome(command.action, await retryAwsConnection(workspaceId));
        case "remove":
          return connectionOutcome(command.action, await removeAwsConnection(workspaceId));
      }
    },
    onSuccess: (outcome) => {
      queryClient.setQueryData(computeQueryKeys.awsConnection(workspaceId), outcome.connection);

      if (outcome.action === "create" || outcome.action === "reconnect") {
        completeAwsAuthorizationHandoff(outcome.authorizationPopup, outcome.authorizationUrl);
      }

      if (outcome.action === "remove") {
        setRemoveOpen(false);
        void Promise.all([
          queryClient.invalidateQueries({
            queryKey: computeQueryKeys.awsConnection(workspaceId),
          }),
          queryClient.invalidateQueries({
            queryKey: computeQueryKeys.policy(workspaceId),
          }),
          queryClient.invalidateQueries({
            queryKey: computeQueryKeys.instances(workspaceId),
          }),
        ]);
      }

      onClose();
    },
    onError: (_error, command) => {
      if (command.action === "create" || command.action === "reconnect") {
        command.authorizationPopup.close();
      }
    },
    onSettled: () => {
      activeActionRef.current = null;
      setActiveAction(null);
    },
  });

  const run = (command: AwsConnectionCommand) => {
    if (activeActionRef.current !== null) return;
    activeActionRef.current = command.action;
    setActiveAction(command.action);
    setLastAction(command.action);
    mutation.reset();
    mutation.mutate(command);
  };

  const runCreate = (accountId: string) => {
    if (activeActionRef.current !== null) return;
    run({
      action: "create",
      accountId,
      authorizationPopup: openAuthorizationPopup(),
    });
  };

  const runReconnect = () => {
    if (activeActionRef.current !== null) return;
    run({
      action: "reconnect",
      authorizationPopup: openAuthorizationPopup(),
    });
  };

  return {
    activeAction,
    createError: lastAction === "create" ? mutation.error : null,
    recoveryError:
      lastAction !== null && lastAction !== "create" && lastAction !== "remove"
        ? mutation.error
        : null,
    removalError: lastAction === "remove" ? mutation.error : null,
    removeOpen,
    setRemoveOpen,
    create: (accountId) => runCreate(accountId.trim()),
    validate: () => run({ action: "validate" }),
    reconnect: runReconnect,
    cancelReconnect: () => run({ action: "cancel_reconnect" }),
    retry: () => run({ action: "retry" }),
    remove: () => run({ action: "remove" }),
  };
}

function connectionOutcome(
  action: Exclude<AwsConnectionDialogAction, "create" | "reconnect">,
  connection: AwsConnection | null,
): AwsConnectionMutationOutcome {
  return {
    action,
    connection,
    authorizationPopup: null,
    authorizationUrl: null,
  };
}

function openAwsAuthorizationPopup(): AwsAuthorizationPopup {
  const authorizationWindow = globalThis.open("about:blank", "_blank");
  return {
    close: () => authorizationWindow?.close(),
    handoff: (authorizationUrl) => {
      if (authorizationWindow) {
        authorizationWindow.opener = null;
        authorizationWindow.location.replace(authorizationUrl);
        return;
      }
      globalThis.open(authorizationUrl, "_blank", "noopener,noreferrer");
    },
  };
}

function completeAwsAuthorizationHandoff(
  authorizationPopup: AwsAuthorizationPopup | null,
  authorizationUrl: string | null,
) {
  if (!authorizationUrl) {
    authorizationPopup?.close();
    return;
  }
  authorizationPopup?.handoff(authorizationUrl);
}

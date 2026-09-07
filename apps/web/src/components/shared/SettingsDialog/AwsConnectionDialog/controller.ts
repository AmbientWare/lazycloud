import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import type { AwsConnection } from "@/lib/api/schemas";
import {
  accountComputeQueryKeys,
  cancelAwsConnectionReconnect,
  createAwsConnection,
  reconnectAwsConnection,
  removeAwsConnection,
  retryAwsConnection,
  validateAwsConnection,
} from "@/lib/queries/compute";

import type { AwsConnectionDialogRecoveryAction } from "./lifecycle";

export type AwsConnectionDialogAction = "create" | AwsConnectionDialogRecoveryAction | "remove";

type AuthorizationCommand =
  | {
      action: "create";
      accountId: string;
      maxCpuInstances: number | null;
      maxGpuInstances: number | null;
    }
  | { action: "reconnect" };

type AwsConnectionCommand =
  AuthorizationCommand | { action: "validate" | "cancel_reconnect" | "retry" | "remove" };

type AwsConnectionMutationOutcome = {
  action: AwsConnectionDialogAction;
  connection: AwsConnection | null;
};

export type AwsConnectionController = {
  activeAction: AwsConnectionDialogAction | null;
  createError: Error | null;
  recoveryError: Error | null;
  removalError: Error | null;
  removeOpen: boolean;
  setRemoveOpen: (open: boolean) => void;
  create: (
    accountId: string,
    maxCpuInstances: number | null,
    maxGpuInstances: number | null,
  ) => void;
  validate: () => void;
  reconnect: () => void;
  cancelReconnect: () => void;
  retry: () => void;
  remove: () => void;
};

export function useAwsConnectionController({
  onClose,
}: {
  onClose: () => void;
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
          const result = await createAwsConnection({
            accountId: command.accountId,
            maxCpuInstances: command.maxCpuInstances,
            maxGpuInstances: command.maxGpuInstances,
          });
          return {
            action: command.action,
            connection: result.connection,
          };
        }
        case "validate":
          return connectionOutcome(command.action, await validateAwsConnection());
        case "reconnect": {
          const result = await reconnectAwsConnection();
          return {
            action: command.action,
            connection: result.connection,
          };
        }
        case "cancel_reconnect":
          return connectionOutcome(command.action, await cancelAwsConnectionReconnect());
        case "retry":
          return connectionOutcome(command.action, await retryAwsConnection());
        case "remove":
          return connectionOutcome(command.action, await removeAwsConnection());
      }
    },
    onSuccess: (outcome) => {
      queryClient.setQueryData(accountComputeQueryKeys.awsConnection(), outcome.connection);

      if (outcome.action === "remove") {
        setRemoveOpen(false);
        // The whole account-level compute root: the connection is gone, and so is
        // the capacity, the inventory, and the catalog that were read through it.
        void queryClient.invalidateQueries({ queryKey: accountComputeQueryKeys.root() });
      }

      if (outcome.action !== "create" && outcome.action !== "reconnect") onClose();
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

  const runCreate = (
    accountId: string,
    maxCpuInstances: number | null,
    maxGpuInstances: number | null,
  ) => {
    if (activeActionRef.current !== null) return;
    run({
      action: "create",
      accountId,
      maxCpuInstances,
      maxGpuInstances,
    });
  };

  const runReconnect = () => {
    if (activeActionRef.current !== null) return;
    run({
      action: "reconnect",
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
    create: (accountId, maxCpuInstances, maxGpuInstances) =>
      runCreate(accountId.trim(), maxCpuInstances, maxGpuInstances),
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
  };
}

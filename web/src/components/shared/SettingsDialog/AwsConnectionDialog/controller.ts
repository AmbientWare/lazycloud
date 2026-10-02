import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import type { Schemas } from "@/lib/api/client";
import {
  cancelAwsConnectionReconnect,
  createAwsConnection,
  reconnectAwsConnection,
  removeAwsConnection,
  retryAwsConnection,
  validateAwsConnection,
} from "@/lib/queries/compute";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

import type { AwsConnectionDialogRecoveryAction } from "./lifecycle";

export type AwsConnectionDialogAction = "create" | AwsConnectionDialogRecoveryAction | "remove";

type AwsConnectionCommand =
  | { action: "create"; accountId: string }
  | { action: Exclude<AwsConnectionDialogAction, "create"> };

type AwsConnectionMutationOutcome = {
  action: AwsConnectionDialogAction;
  connection: Schemas["AwsConnection"] | null;
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
      const { action } = command;
      switch (action) {
        case "create":
          return { action, connection: (await createAwsConnection(command.accountId)).connection };
        case "reconnect":
          return { action, connection: (await reconnectAwsConnection()).connection };
        case "validate":
          return { action, connection: await validateAwsConnection() };
        case "cancel_reconnect":
          return { action, connection: await cancelAwsConnectionReconnect() };
        case "retry":
          return { action, connection: await retryAwsConnection() };
        case "remove":
          return { action, connection: await removeAwsConnection() };
      }
    },
    onSuccess: (outcome) => {
      queryClient.setQueryData(accountQueryKeys.compute.awsConnection(), outcome.connection);

      if (outcome.action === "remove") {
        setRemoveOpen(false);
        // The whole account-level compute root: the connection is gone, and so is
        // the capacity, the inventory, and the catalog that were read through it.
        void queryClient.invalidateQueries({ queryKey: accountQueryKeys.compute.root() });
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
    create: (accountId) => run({ action: "create", accountId: accountId.trim() }),
    validate: () => run({ action: "validate" }),
    reconnect: () => run({ action: "reconnect" }),
    cancelReconnect: () => run({ action: "cancel_reconnect" }),
    retry: () => run({ action: "retry" }),
    remove: () => run({ action: "remove" }),
  };
}

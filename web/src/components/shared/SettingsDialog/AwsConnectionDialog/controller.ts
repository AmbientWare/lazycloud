import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import type { Schemas } from "@/lib/api/client";
import {
  cancelAwsConnectionReconnect,
  createAwsConnection,
  forgetAwsConnection,
  reconnectAwsConnection,
  removeAwsConnection,
  retryAwsConnection,
  validateAwsConnection,
} from "@/lib/queries/compute";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

import type { AwsConnectionDialogRecoveryAction } from "./lifecycle";

/** The actions the dialog confirms before running. */
export type AwsConnectionConfirmedAction = "remove" | "forget";

export type AwsConnectionDialogAction =
  "create" | AwsConnectionDialogRecoveryAction | AwsConnectionConfirmedAction;

type AwsConnectionCommand =
  | { action: "create"; accountId: string }
  | { action: Exclude<AwsConnectionDialogAction, "create"> };

type AwsConnectionMutationOutcome =
  | { action: "forget"; leftInAws: Schemas["AwsLeftover"][] }
  | {
      action: Exclude<AwsConnectionDialogAction, "forget">;
      connection: Schemas["AwsConnection"] | null;
    };

export type AwsConnectionController = {
  activeAction: AwsConnectionDialogAction | null;
  createError: Error | null;
  recoveryError: Error | null;
  confirmError: Error | null;
  confirming: AwsConnectionConfirmedAction | null;
  setConfirming: (action: AwsConnectionConfirmedAction | null) => void;
  /** What a forgotten connection left in AWS; null until a forget succeeds. */
  leftInAws: Schemas["AwsLeftover"][] | null;
  create: (accountId: string) => void;
  validate: () => void;
  reconnect: () => void;
  cancelReconnect: () => void;
  retry: () => void;
  remove: () => void;
  forget: () => void;
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
  const [confirming, setConfirming] = useState<AwsConnectionConfirmedAction | null>(null);
  const [leftInAws, setLeftInAws] = useState<Schemas["AwsLeftover"][] | null>(null);

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
        case "forget":
          return { action, leftInAws: (await forgetAwsConnection()).left_in_aws };
      }
    },
    onSuccess: (outcome) => {
      queryClient.setQueryData(
        accountQueryKeys.compute.awsConnection(),
        outcome.action === "forget" ? null : outcome.connection,
      );
      if (outcome.action === "remove" || outcome.action === "forget") {
        setConfirming(null);
        // The whole account-level compute root: the connection is gone or going,
        // and so is the capacity, the inventory, and the catalog read through it.
        void queryClient.invalidateQueries({ queryKey: accountQueryKeys.compute.root() });
      }
      if (outcome.action === "forget") {
        // The dialog stays open to list what the customer deletes in AWS.
        setLeftInAws(outcome.leftInAws);
        return;
      }
      // Creating and reconnecting leave the dialog open on the AWS Console link.
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

  const confirmed = lastAction === "remove" || lastAction === "forget";
  return {
    activeAction,
    createError: lastAction === "create" ? mutation.error : null,
    recoveryError:
      lastAction !== null && lastAction !== "create" && !confirmed ? mutation.error : null,
    confirmError: confirmed ? mutation.error : null,
    confirming,
    setConfirming,
    leftInAws,
    create: (accountId) => run({ action: "create", accountId: accountId.trim() }),
    validate: () => run({ action: "validate" }),
    reconnect: () => run({ action: "reconnect" }),
    cancelReconnect: () => run({ action: "cancel_reconnect" }),
    retry: () => run({ action: "retry" }),
    remove: () => run({ action: "remove" }),
    forget: () => run({ action: "forget" }),
  };
}

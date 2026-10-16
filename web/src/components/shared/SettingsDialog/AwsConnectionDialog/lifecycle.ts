import type { Schemas } from "@/lib/api/client";

type AwsConnection = Schemas["AwsConnection"];
type AwsConnectionAction = Schemas["AwsConnectionAction"];

export type AwsConnectionPresentation = {
  label:
    | "Authorization required"
    | "Checking"
    | "Connected"
    | "Needs attention"
    | "Removing"
    | "Action required";
  live: boolean;
};

/** Authorizing is the console link; removing and forgetting ask for confirmation first. */
export type AwsConnectionDialogRecoveryAction = Exclude<
  AwsConnectionAction,
  "authorize" | "remove" | "forget"
>;

export type AwsConnectionDialogActionPlan = {
  /** In the API's order; the first is the primary action. */
  recovery: AwsConnectionDialogRecoveryAction[];
  remove: boolean;
  forget: boolean;
};

export function awsConnectionPresentation(connection: AwsConnection): AwsConnectionPresentation {
  switch (connection.phase) {
    case "awaiting_authorization":
      return { label: "Authorization required", live: false };
    case "validating":
    case "reconnect_pending":
    case "retiring_authorization":
      return { label: "Checking", live: false };
    case "ready":
      return { label: "Connected", live: true };
    case "degraded":
      return { label: "Needs attention", live: false };
    case "disconnect_draining":
    case "revoking":
    case "verifying_revocation":
      return { label: "Removing", live: false };
    case "action_required":
      return { label: "Action required", live: false };
  }
}

/** The dialog offers exactly the actions the API allows in the connection's current state. */
export function awsConnectionDialogActionPlan(
  connection: AwsConnection,
): AwsConnectionDialogActionPlan {
  const actions = connection.available_actions;
  return {
    recovery: actions.filter(isRecoveryAction),
    remove: actions.includes("remove"),
    forget: actions.includes("forget"),
  };
}

function isRecoveryAction(
  action: AwsConnectionAction,
): action is AwsConnectionDialogRecoveryAction {
  switch (action) {
    case "validate":
    case "reconnect":
    case "cancel_reconnect":
    case "retry":
      return true;
    case "authorize":
    case "remove":
    case "forget":
      return false;
  }
}

export function awsConnectionIsUsable(connection: AwsConnection): boolean {
  return connection.hosts_workloads || connection.can_manage_existing_capacity;
}

export function awsConnectionIsRemoving(connection: AwsConnection): boolean {
  return awsConnectionPresentation(connection).label === "Removing";
}

export type AwsConnectionConfirmation = {
  title: string;
  description: string;
  cancelLabel: string;
  confirmLabel: string;
};

export function awsForgetConfirmation(connection: AwsConnection): AwsConnectionConfirmation {
  return {
    title: "Forget AWS connection?",
    description: `LazyCloud stops managing account ${connection.account_id} and removes the connection. Anything it could not delete stays in AWS, and the next step links each one for you to delete.`,
    cancelLabel: "Keep connection",
    confirmLabel: "Forget connection",
  };
}

export function awsRemovalConfirmation(connection: AwsConnection): AwsConnectionConfirmation {
  if (!connection.active_authorization) {
    return {
      title: "Cancel AWS setup?",
      description: "This unfinished connection will be removed. No AWS compute has been activated.",
      cancelLabel: "Keep setup",
      confirmLabel: "Cancel setup",
    };
  }
  return {
    title: "Remove AWS connection?",
    description:
      "Disconnecting is refused while any workspace lives in this account; delete those workspaces first. LazyCloud terminates its instances and deletes the connection stacks, then removes the connection.",
    cancelLabel: "Keep connected",
    confirmLabel: "Remove connection",
  };
}

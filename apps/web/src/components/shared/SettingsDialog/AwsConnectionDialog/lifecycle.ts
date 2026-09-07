import type { AwsConnection, AwsConnectionAction } from "@/lib/api/schemas";

export type AwsConnectionPresentation = {
  label:
    | "Authorization required"
    | "Checking"
    | "Connected"
    | "Needs attention"
    | "Removing"
    | "Action required";
};

export type AwsConnectionDialogRecoveryAction = Exclude<
  AwsConnectionAction,
  "authorize" | "remove"
>;

export type AwsConnectionDialogActionPlan = {
  primary: AwsConnectionDialogRecoveryAction | null;
  secondary: AwsConnectionDialogRecoveryAction | null;
  destructive: "remove" | null;
};

export function awsConnectionPresentation(connection: AwsConnection): AwsConnectionPresentation {
  switch (connection.phase) {
    case "awaiting_authorization":
      return { label: "Authorization required" };
    case "validating":
    case "reconnect_pending":
    case "retiring_authorization":
      return { label: "Checking" };
    case "ready":
      return { label: "Connected" };
    case "degraded":
      return { label: "Needs attention" };
    case "disconnect_draining":
    case "revoking":
    case "verifying_revocation":
      return { label: "Removing" };
    case "action_required":
      return { label: "Action required" };
  }
}

export function awsConnectionDialogDescription(connection: AwsConnection): string {
  switch (connection.phase) {
    case "awaiting_authorization":
      return "Complete authorization in AWS or cancel this unfinished setup.";
    case "validating":
      return "Checking AWS authorization.";
    case "ready":
      return "AWS compute placement is active for this account.";
    case "degraded":
      return "Check existing access or start a replacement authorization.";
    case "reconnect_pending":
      return "Current capacity stays active while you authorize replacement access.";
    case "retiring_authorization":
      return "Replacement access is active. Removing the previous authorization.";
    case "disconnect_draining":
    case "revoking":
    case "verifying_revocation":
      return "Removing AWS resources and authorization.";
    case "action_required":
      return "Automatic AWS cleanup needs a recovery action.";
  }
}

function hasAwsConnectionAction(connection: AwsConnection, action: AwsConnectionAction): boolean {
  return connection.available_actions.includes(action);
}

export function awsConnectionDialogActionPlan(
  connection: AwsConnection,
): AwsConnectionDialogActionPlan {
  const supports = (action: AwsConnectionAction) => hasAwsConnectionAction(connection, action);

  switch (connection.phase) {
    case "awaiting_authorization":
    case "validating":
      return {
        primary: null,
        secondary: null,
        destructive: supports("remove") ? "remove" : null,
      };
    case "ready":
      return {
        primary: null,
        secondary: null,
        destructive: supports("remove") ? "remove" : null,
      };
    case "degraded": {
      const primary = supports("validate")
        ? "validate"
        : supports("reconnect")
          ? "reconnect"
          : null;
      return {
        primary,
        secondary: primary === "validate" && supports("reconnect") ? "reconnect" : null,
        destructive: supports("remove") ? "remove" : null,
      };
    }
    case "reconnect_pending":
      return {
        primary: null,
        secondary: supports("cancel_reconnect") ? "cancel_reconnect" : null,
        destructive: null,
      };
    case "action_required":
      return {
        primary: supports("retry") ? "retry" : null,
        secondary: null,
        destructive: null,
      };
    case "retiring_authorization":
    case "disconnect_draining":
    case "revoking":
    case "verifying_revocation":
      return { primary: null, secondary: null, destructive: null };
  }
}

export function awsConnectionIsUsable(connection: AwsConnection): boolean {
  return connection.hosts_workloads || connection.can_manage_existing_capacity;
}

export function awsConnectionIsRemoving(connection: AwsConnection): boolean {
  return awsConnectionPresentation(connection).label === "Removing";
}

export function awsRemovalConfirmation(connection: AwsConnection): {
  title: string;
  description: string;
  cancelLabel: string;
  confirmLabel: string;
} {
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
      "AWS placement will stop immediately. Running compute will drain before managed resources and authorization are removed.",
    cancelLabel: "Keep connected",
    confirmLabel: "Remove connection",
  };
}

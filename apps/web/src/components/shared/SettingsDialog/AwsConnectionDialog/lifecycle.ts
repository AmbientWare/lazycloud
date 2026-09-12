import type { AwsConnection, AwsConnectionAction } from "@/lib/api/schemas";

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

export function awsConnectionDialogDescription(connection: AwsConnection): string {
  switch (connection.phase) {
    case "awaiting_authorization":
      return "Authorize access in AWS to finish connecting your account.";
    case "validating":
      return "Checking AWS authorization.";
    case "ready":
      return "Workloads can run in your AWS account.";
    case "degraded":
      return "Check authorization or reconnect your AWS account.";
    case "reconnect_pending":
      return "Existing instances keep running while you authorize access again.";
    case "retiring_authorization":
      return "New access is ready. Removing the previous authorization.";
    case "disconnect_draining":
    case "revoking":
    case "verifying_revocation":
      return "Removing AWS resources and authorization.";
    case "action_required":
      return "AWS cleanup failed. Review the error and retry.";
  }
}

export function awsConnectionDialogActionPlan(
  connection: AwsConnection,
): AwsConnectionDialogActionPlan {
  const supports = (action: AwsConnectionAction) => connection.available_actions.includes(action);

  switch (connection.phase) {
    case "awaiting_authorization":
    case "validating":
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

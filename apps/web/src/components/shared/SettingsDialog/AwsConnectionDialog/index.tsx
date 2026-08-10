import { useState } from "react";
import {
  Cloud,
  ExternalLink,
  Loader2,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";

import { StatusChip } from "@/components/shared/StatusChip";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { AwsConnection } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";

import { useAwsConnectionController } from "./controller";
import {
  type AwsConnectionDialogRecoveryAction,
  awsConnectionDialogActionPlan,
  awsConnectionDialogDescription,
  awsConnectionPresentation,
  awsRemovalConfirmation,
} from "./lifecycle";

const AWS_CONNECT_FORM_ID = "aws-connect-form";

export function AwsConnectionDialog({
  connection,
  open,
  onOpenChange,
}: {
  connection: AwsConnection | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open ? (
        <AwsConnectionFlow connection={connection} onClose={() => onOpenChange(false)} />
      ) : null}
    </Dialog>
  );
}

function AwsConnectionFlow({
  connection,
  onClose,
}: {
  connection: AwsConnection | null;
  onClose: () => void;
}) {
  const [accountId, setAccountId] = useState("");
  const controller = useAwsConnectionController({ onClose });

  const pendingAction: AwsConnectionDialogRecoveryAction | null =
    controller.activeAction !== "create" && controller.activeAction !== "remove"
      ? controller.activeAction
      : null;
  const accountValid = /^\d{12}$/.test(accountId);

  return (
    <>
      <DialogContent className="flex max-h-[min(42rem,calc(100svh-2rem))] max-w-xl flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <DialogHeader className="shrink-0 border-b border-border bg-muted/20 px-5 py-4 pr-12 text-left">
          <DialogTitle className="flex items-center gap-2 text-base">
            <Cloud className="size-4 text-brand" aria-hidden="true" />
            {connection ? "AWS connection" : "Connect AWS"}
          </DialogTitle>
          <DialogDescription>
            {connection
              ? awsConnectionDialogDescription(connection)
              : "Authorize LazyCloud to provision compute in your AWS account."}
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {connection ? (
            <ConnectionActions
              connection={connection}
              pendingAction={pendingAction}
              error={controller.recoveryError}
              onClose={onClose}
              onValidate={controller.validate}
              onReconnect={controller.reconnect}
              onCancelReconnect={controller.cancelReconnect}
              onRetry={controller.retry}
              onRemove={() => controller.setRemoveOpen(true)}
            />
          ) : (
            <ConnectForm
              accountId={accountId}
              error={controller.createError}
              onAccountIdChange={setAccountId}
              onSubmit={() => controller.create(accountId)}
            />
          )}
        </div>

        {!connection ? (
          <DialogFooter className="shrink-0 border-t border-border bg-muted/20 px-5 py-3">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              form={AWS_CONNECT_FORM_ID}
              disabled={!accountValid || controller.activeAction !== null}
            >
              {controller.activeAction === "create" ? (
                <Loader2 className="animate-spin" />
              ) : (
                <ShieldCheck />
              )}
              Continue to AWS
            </Button>
          </DialogFooter>
        ) : null}
      </DialogContent>

      {connection ? (
        <RemoveConnectionDialog
          connection={connection}
          open={controller.removeOpen}
          pending={controller.activeAction === "remove"}
          error={controller.removalError}
          onOpenChange={controller.setRemoveOpen}
          onConfirm={controller.remove}
        />
      ) : null}
    </>
  );
}

function ConnectForm({
  accountId,
  error,
  onAccountIdChange,
  onSubmit,
}: {
  accountId: string;
  error: Error | null;
  onAccountIdChange: (accountId: string) => void;
  onSubmit: () => void;
}) {
  return (
    <form
      id={AWS_CONNECT_FORM_ID}
      className="space-y-5"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div>
        <label htmlFor="aws-account-id" className="micro-label">
          AWS account ID
        </label>
        <Input
          id="aws-account-id"
          value={accountId}
          onChange={(event) =>
            onAccountIdChange(event.target.value.replace(/\D/g, "").slice(0, 12))
          }
          placeholder="123456789012"
          inputMode="numeric"
          autoComplete="off"
          className="mt-1.5 font-mono"
          autoFocus
        />
        <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
          Resources stay in this account and are billed directly by AWS.
        </p>
      </div>

      <div className="flex items-start gap-3 border border-border bg-card p-3">
        <ShieldCheck className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" />
        <div>
          <h3 className="text-sm font-medium">One-time AWS authorization</h3>
          <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
            You will review the requested access in AWS. Access and secret keys are never requested.
          </p>
        </div>
      </div>

      {error ? <ErrorNotice error={error} /> : null}
    </form>
  );
}

function ConnectionActions({
  connection,
  pendingAction,
  error,
  onClose,
  onValidate,
  onReconnect,
  onCancelReconnect,
  onRetry,
  onRemove,
}: {
  connection: AwsConnection;
  pendingAction: AwsConnectionDialogRecoveryAction | null;
  error: Error | null;
  onClose: () => void;
  onValidate: () => void;
  onReconnect: () => void;
  onCancelReconnect: () => void;
  onRetry: () => void;
  onRemove: () => void;
}) {
  const presentation = awsConnectionPresentation(connection);
  const actionPlan = awsConnectionDialogActionPlan(connection);
  const lastValidatedAt = connection.active_authorization?.last_validated_at;
  const customerAction = connection.customer_action;
  const customerActionAvailable = Boolean(customerAction?.url);
  const pending = pendingAction !== null;
  const showCapacity = connection.hosts_workloads || connection.can_manage_existing_capacity;
  const showActionBar =
    actionPlan.primary !== null || actionPlan.secondary !== null || actionPlan.destructive !== null;

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-sm font-medium">Account {connection.account_id}</h3>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{connection.detail}</p>
        </div>
        <StatusChip status={presentation.label} live={presentation.live} />
      </div>

      {showCapacity ? (
        <dl className="border border-border bg-card text-xs">
          <ConnectionDetail
            label="Placement"
            value={connection.hosts_workloads ? "Available" : "Unavailable"}
          />
          <ConnectionDetail
            label="Active compute"
            value={connection.can_manage_existing_capacity ? "Managed" : "Unavailable"}
          />
          <ConnectionDetail
            label="Last validated"
            value={lastValidatedAt ? relativeTime(lastValidatedAt) : "Not yet"}
          />
        </dl>
      ) : null}

      {customerAction?.url ? (
        <Button asChild className="w-full">
          <a href={customerAction.url} target="_blank" rel="noreferrer" onClick={onClose}>
            {customerAction.label}
            <ExternalLink />
          </a>
        </Button>
      ) : null}

      {error ? <ErrorNotice error={error} /> : null}

      {showActionBar ? (
        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
          {actionPlan.primary ? (
            <ConnectionRecoveryButton
              action={actionPlan.primary}
              pendingAction={pendingAction}
              disabled={pending}
              variant={customerActionAvailable ? "outline" : "default"}
              onValidate={onValidate}
              onReconnect={onReconnect}
              onCancelReconnect={onCancelReconnect}
              onRetry={onRetry}
            />
          ) : null}
          {actionPlan.secondary ? (
            <ConnectionRecoveryButton
              action={actionPlan.secondary}
              pendingAction={pendingAction}
              disabled={pending}
              variant="outline"
              onValidate={onValidate}
              onReconnect={onReconnect}
              onCancelReconnect={onCancelReconnect}
              onRetry={onRetry}
            />
          ) : null}
          {actionPlan.destructive ? (
            <Button
              type="button"
              variant="ghost"
              className="ml-auto text-destructive"
              disabled={pending}
              onClick={onRemove}
            >
              <Trash2 />
              {connection.active_authorization ? "Remove connection" : "Cancel setup"}
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ConnectionRecoveryButton({
  action,
  pendingAction,
  disabled,
  variant,
  onValidate,
  onReconnect,
  onCancelReconnect,
  onRetry,
}: {
  action: AwsConnectionDialogRecoveryAction;
  pendingAction: AwsConnectionDialogRecoveryAction | null;
  disabled: boolean;
  variant: "default" | "outline";
  onValidate: () => void;
  onReconnect: () => void;
  onCancelReconnect: () => void;
  onRetry: () => void;
}) {
  const controls = {
    validate: {
      label: "Check authorization",
      icon: RefreshCw,
      onClick: onValidate,
    },
    reconnect: { label: "Reconnect", icon: ShieldCheck, onClick: onReconnect },
    cancel_reconnect: {
      label: "Cancel reconnect",
      icon: X,
      onClick: onCancelReconnect,
    },
    retry: { label: "Retry", icon: RotateCcw, onClick: onRetry },
  } satisfies Record<
    AwsConnectionDialogRecoveryAction,
    { label: string; icon: typeof RefreshCw; onClick: () => void }
  >;
  const control = controls[action];
  const Icon = control.icon;
  const isPending = pendingAction === action;

  return (
    <Button type="button" variant={variant} disabled={disabled} onClick={control.onClick}>
      {isPending ? <Loader2 className="animate-spin" /> : <Icon />}
      {control.label}
    </Button>
  );
}

function RemoveConnectionDialog({
  connection,
  open,
  pending,
  error,
  onOpenChange,
  onConfirm,
}: {
  connection: AwsConnection;
  open: boolean;
  pending: boolean;
  error: Error | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const confirmation = awsRemovalConfirmation(connection);
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{confirmation.title}</AlertDialogTitle>
          <AlertDialogDescription>{confirmation.description}</AlertDialogDescription>
        </AlertDialogHeader>
        {error ? <ErrorNotice error={error} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>{confirmation.cancelLabel}</AlertDialogCancel>
          <Button variant="destructive" disabled={pending} onClick={onConfirm}>
            {pending ? <Loader2 className="animate-spin" /> : <Trash2 />}
            {confirmation.confirmLabel}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function ConnectionDetail({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1 border-b border-border px-3 py-2.5 last:border-b-0 sm:grid-cols-[8rem_minmax(0,1fr)]">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-foreground">{value}</dd>
    </div>
  );
}

function ErrorNotice({ error }: { error: Error }) {
  return (
    <p
      className="border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive"
      role="alert"
    >
      {error.message}
    </p>
  );
}

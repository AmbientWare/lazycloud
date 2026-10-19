import { useState, type ReactNode } from "react";
import {
  Check,
  Cloud,
  CloudOff,
  ExternalLink,
  Loader2,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
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
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { Schemas } from "@/lib/api/client";
import { cn } from "@/lib/utils";

import { type AwsConnectionConfirmedAction, useAwsConnectionController } from "./controller";
import {
  type AwsConnectionDialogRecoveryAction,
  awsConnectionDialogActionPlan,
  awsConnectionPresentation,
  awsForgetConfirmation,
  awsRemovalConfirmation,
  awsSetupStep,
} from "./lifecycle";

type AwsConnection = Schemas["AwsConnection"];

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
    controller.activeAction !== "create" &&
    controller.activeAction !== "remove" &&
    controller.activeAction !== "forget"
      ? controller.activeAction
      : null;
  const accountValid = /^\d{12}$/.test(accountId);
  const leftInAws = controller.leftInAws;
  const connecting = !connection && leftInAws === null;

  return (
    <>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[min(42rem,calc(100svh-2rem))] max-w-xl flex-col gap-0 overflow-hidden p-0 sm:max-w-xl"
      >
        <DialogHeader className="shrink-0 border-b border-border bg-muted/20 px-5 py-4 pr-12 text-left">
          <DialogTitle className="flex items-center gap-2 text-base">
            <Cloud className="size-4 text-brand" aria-hidden="true" />
            {leftInAws !== null
              ? "AWS connection forgotten"
              : connection
                ? "AWS connection"
                : "Connect AWS"}
          </DialogTitle>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {leftInAws !== null ? (
            <LeftInAws leftovers={leftInAws} />
          ) : connection ? (
            <ConnectionActions
              connection={connection}
              pendingAction={pendingAction}
              error={controller.recoveryError}
              onValidate={controller.validate}
              onReconnect={controller.reconnect}
              onCancelReconnect={controller.cancelReconnect}
              onRetry={controller.retry}
              onConfirm={controller.setConfirming}
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

        {connecting ? (
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
              Create connection
            </Button>
          </DialogFooter>
        ) : null}

        {leftInAws !== null ? (
          <DialogFooter className="shrink-0 border-t border-border bg-muted/20 px-5 py-3">
            <Button type="button" onClick={onClose}>
              Done
            </Button>
          </DialogFooter>
        ) : null}
      </DialogContent>

      {connection ? (
        <ConfirmConnectionDialog
          connection={connection}
          action={controller.confirming}
          pending={controller.activeAction === "remove" || controller.activeAction === "forget"}
          error={controller.confirmError}
          onCancel={() => controller.setConfirming(null)}
          onRemove={controller.remove}
          onForget={controller.forget}
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
          AWS bills you for what runs here. A workspace created in this account keeps its compute
          and volumes there for good, and you delete those workspaces before you can disconnect.
        </p>
      </div>

      <p className="text-xs leading-5 text-muted-foreground">
        Next, you review the requested access once in the AWS Console. LazyCloud never asks for
        access or secret keys.
      </p>

      {error ? <ErrorNotice error={error} /> : null}
    </form>
  );
}

function ConnectionActions({
  connection,
  pendingAction,
  error,
  onValidate,
  onReconnect,
  onCancelReconnect,
  onRetry,
  onConfirm,
}: {
  connection: AwsConnection;
  pendingAction: AwsConnectionDialogRecoveryAction | null;
  error: Error | null;
  onValidate: () => void;
  onReconnect: () => void;
  onCancelReconnect: () => void;
  onRetry: () => void;
  onConfirm: (action: AwsConnectionConfirmedAction) => void;
}) {
  const presentation = awsConnectionPresentation(connection);
  const actionPlan = awsConnectionDialogActionPlan(connection);
  const lastValidatedAt = connection.active_authorization?.last_validated_at;
  const customerAction = connection.customer_action;
  const authorizing = connection.available_actions.includes("authorize");
  const pending = pendingAction !== null;
  const showCapacity = connection.hosts_workloads || connection.can_manage_existing_capacity;
  const showActionBar = actionPlan.recovery.length > 0 || actionPlan.remove || actionPlan.forget;
  const setupStep = awsSetupStep(connection);

  if (setupStep) {
    const checking = connection.phase === "validating" || pendingAction === "validate";
    const checkedAt = connection.pending_authorization?.last_validation_started_at;
    return (
      <div className="space-y-5">
        <div className="flex items-center justify-between gap-4">
          <h3 className="text-sm font-medium">
            Account <span className="mono">{connection.account_id}</span>
          </h3>
          <StatusChip status={presentation.label} live={presentation.live} />
        </div>
        <SetupTrack connection={connection} step={setupStep} />
        {error ? <ErrorNotice error={error} /> : null}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border pt-4">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={pending || checking}
            onClick={onValidate}
          >
            {checking ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            Check now
          </Button>
          <span className="text-xs text-muted-foreground">
            {checking ? (
              "Checking"
            ) : checkedAt ? (
              <>
                Checked <LiveRelativeTime value={checkedAt} />
              </>
            ) : (
              "Not checked yet"
            )}
          </span>
          {actionPlan.recovery.includes("cancel_reconnect") ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="ml-auto"
              disabled={pending}
              onClick={onCancelReconnect}
            >
              {pendingAction === "cancel_reconnect" ? <Loader2 className="animate-spin" /> : <X />}
              Cancel reconnect
            </Button>
          ) : actionPlan.remove ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="ml-auto text-destructive"
              disabled={pending}
              onClick={() => onConfirm("remove")}
            >
              <Trash2 />
              Cancel setup
            </Button>
          ) : null}
        </div>
      </div>
    );
  }

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
            label="New workloads"
            value={connection.hosts_workloads ? "Available" : "Unavailable"}
          />
          <ConnectionDetail
            label="Existing instances"
            value={connection.can_manage_existing_capacity ? "Managed" : "Unavailable"}
          />
          <ConnectionDetail
            label="Last validated"
            value={lastValidatedAt ? <LiveRelativeTime value={lastValidatedAt} /> : "Not yet"}
          />
        </dl>
      ) : null}

      {showCapacity && connection.blocked_quotas?.length ? (
        <BlockedQuotas quotas={connection.blocked_quotas} />
      ) : null}

      {customerAction ? (
        <div className="space-y-3 border border-border bg-card p-4 text-xs">
          <p className="leading-5">
            {authorizing
              ? "In the AWS Console, review the stack, tick the IAM acknowledgement and choose Create stack. This dialog updates by itself once the stack is ready."
              : customerAction.label}
          </p>
          <Button asChild className="w-full">
            <a href={customerAction.url} target="_blank" rel="noopener">
              Open AWS Console
              <ExternalLink />
            </a>
          </Button>
        </div>
      ) : null}

      {error ? <ErrorNotice error={error} /> : null}

      {showActionBar ? (
        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
          {actionPlan.recovery.map((action, index) => (
            <ConnectionRecoveryButton
              key={action}
              action={action}
              pendingAction={pendingAction}
              disabled={pending}
              variant={index === 0 && !customerAction ? "default" : "outline"}
              onValidate={onValidate}
              onReconnect={onReconnect}
              onCancelReconnect={onCancelReconnect}
              onRetry={onRetry}
            />
          ))}
          {actionPlan.forget ? (
            <Button
              type="button"
              variant="ghost"
              className="ml-auto text-destructive"
              disabled={pending}
              onClick={() => onConfirm("forget")}
            >
              <CloudOff />
              Forget connection
            </Button>
          ) : null}
          {actionPlan.remove ? (
            <Button
              type="button"
              variant="ghost"
              className={actionPlan.forget ? "text-destructive" : "ml-auto text-destructive"}
              disabled={pending}
              onClick={() => onConfirm("remove")}
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

const quotaClassLabels: Record<Schemas["AwsBlockedQuota"]["quota_class"], string> = {
  standard: "CPU",
  g: "GPU",
};

const quotaMarketLabels: Record<Schemas["AwsBlockedQuota"]["market"], string> = {
  on_demand: "On-Demand",
  spot: "Spot",
};

/** Quotas too small for one machine, each with the AWS page that raises it. */
function BlockedQuotas({ quotas }: { quotas: Schemas["AwsBlockedQuota"][] }) {
  return (
    <div className="space-y-2 border border-border bg-card p-4 text-xs">
      <p className="leading-5">
        AWS limits this account below the smallest machine LazyCloud buys here. Ask AWS to raise
        these quotas.
      </p>
      <ul className="divide-y divide-border">
        {quotas.map((quota) => (
          <li
            key={`${quota.region}/${quota.quota_class}/${quota.market}`}
            className="flex items-center justify-between gap-3 py-2"
          >
            <span>
              {quotaMarketLabels[quota.market]} {quotaClassLabels[quota.quota_class]} in{" "}
              <span className="mono">{quota.region}</span>: {quota.vcpus} vCPUs, needs{" "}
              {quota.needed_vcpus}
            </span>
            <Button asChild variant="outline" size="sm">
              <a href={quota.request_url} target="_blank" rel="noopener">
                Request more
                <ExternalLink />
              </a>
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The setup's steps in order; the customer's stack decides which one is current. */
function SetupTrack({
  connection,
  step,
}: {
  connection: AwsConnection;
  step: Schemas["AwsSetupStep"];
}) {
  const consoleUrl = connection.console_url;
  const failedStackUrl = step === "stack_failed" ? connection.customer_action?.url : undefined;
  return (
    <ol className="space-y-4">
      <SetupStepRow
        state={step === "waiting_for_stack" ? "current" : "done"}
        marker="1"
        title="Create the stack in AWS"
      >
        {step === "waiting_for_stack" ? (
          <>
            <p>Tick the IAM acknowledgement and choose Create stack.</p>
            {consoleUrl ? (
              <Button asChild size="sm" className="mt-2.5">
                <a href={consoleUrl} target="_blank" rel="noopener">
                  Open AWS Console
                  <ExternalLink />
                </a>
              </Button>
            ) : null}
          </>
        ) : null}
      </SetupStepRow>
      {step === "stack_failed" ? (
        <SetupStepRow state="failed" marker="2" title="The stack failed in AWS">
          {connection.pending_authorization?.error_message ? (
            <p className="mono border border-destructive/40 bg-destructive/5 p-2.5 text-[11px] text-foreground">
              {connection.pending_authorization.error_message}
            </p>
          ) : null}
          <div className="mt-2.5 flex flex-wrap gap-2">
            {failedStackUrl ? (
              <Button asChild variant="outline" size="sm">
                <a href={failedStackUrl} target="_blank" rel="noopener">
                  Delete the stack
                  <ExternalLink />
                </a>
              </Button>
            ) : null}
            {consoleUrl ? (
              <Button asChild size="sm">
                <a href={consoleUrl} target="_blank" rel="noopener">
                  Create again
                  <ExternalLink />
                </a>
              </Button>
            ) : null}
          </div>
        </SetupStepRow>
      ) : (
        <SetupStepRow
          state={step === "stack_creating" ? "working" : "upcoming"}
          marker="2"
          title={step === "stack_creating" ? "AWS is creating the stack" : "AWS creates the stack"}
        >
          {step === "stack_creating" ? (
            <p>
              {connection.pending_authorization?.error_message
                ? `${connection.pending_authorization.error_message}. `
                : null}
              Usually about 3 minutes in all. This updates by itself.
            </p>
          ) : null}
        </SetupStepRow>
      )}
      <SetupStepRow state="upcoming" marker="3" title="LazyCloud checks access" last />
    </ol>
  );
}

type SetupStepState = "done" | "current" | "working" | "failed" | "upcoming";

function SetupStepRow({
  state,
  marker,
  title,
  last = false,
  children,
}: {
  state: SetupStepState;
  marker: string;
  title: string;
  last?: boolean;
  children?: ReactNode;
}) {
  return (
    <li
      className="relative grid grid-cols-[1.5rem_minmax(0,1fr)] gap-x-3.5"
      aria-current={
        state === "current" || state === "working" || state === "failed" ? "step" : undefined
      }
    >
      {last ? null : (
        <span
          aria-hidden="true"
          className={cn(
            "absolute top-7 -bottom-3 left-3 w-px",
            state === "done" ? "bg-brand" : "bg-border",
          )}
        />
      )}
      <span
        aria-hidden="true"
        className={cn(
          "mono flex size-6 items-center justify-center border text-[11px]",
          state === "done" && "border-brand bg-brand text-brand-foreground",
          (state === "current" || state === "working") && "border-brand text-brand",
          state === "failed" && "border-destructive text-destructive",
          state === "upcoming" && "border-border text-muted-foreground/70",
        )}
      >
        {state === "done" ? (
          <Check className="size-3.5" strokeWidth={3} />
        ) : state === "working" ? (
          <Loader2 className="size-3.5 animate-spin" />
        ) : state === "failed" ? (
          <X className="size-3.5" strokeWidth={3} />
        ) : (
          marker
        )}
      </span>
      <div className="min-w-0">
        <p
          className={cn(
            "text-sm leading-6",
            state === "done" && "text-muted-foreground",
            (state === "current" || state === "working") && "font-medium text-foreground",
            state === "failed" && "font-medium text-destructive",
            state === "upcoming" && "text-muted-foreground/70",
          )}
        >
          {title}
        </p>
        {children ? (
          <div className="mt-0.5 text-xs leading-5 text-muted-foreground">{children}</div>
        ) : null}
      </div>
    </li>
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

function ConfirmConnectionDialog({
  connection,
  action,
  pending,
  error,
  onCancel,
  onRemove,
  onForget,
}: {
  connection: AwsConnection;
  action: AwsConnectionConfirmedAction | null;
  pending: boolean;
  error: Error | null;
  onCancel: () => void;
  onRemove: () => void;
  onForget: () => void;
}) {
  // The closing animation keeps showing the action that was being confirmed.
  const [shown, setShown] = useState(action);
  if (action !== null && action !== shown) setShown(action);
  const forgetting = (action ?? shown) === "forget";
  const confirmation = forgetting
    ? awsForgetConfirmation(connection)
    : awsRemovalConfirmation(connection);
  const Icon = forgetting ? CloudOff : Trash2;
  return (
    <AlertDialog
      open={action !== null}
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{confirmation.title}</AlertDialogTitle>
          <AlertDialogDescription>{confirmation.description}</AlertDialogDescription>
        </AlertDialogHeader>
        {error ? <ErrorNotice error={error} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>{confirmation.cancelLabel}</AlertDialogCancel>
          <Button
            variant="destructive"
            disabled={pending}
            onClick={forgetting ? onForget : onRemove}
          >
            {pending ? <Loader2 className="animate-spin" /> : <Icon />}
            {confirmation.confirmLabel}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function LeftInAws({ leftovers }: { leftovers: Schemas["AwsLeftover"][] }) {
  if (leftovers.length === 0) {
    return <p className="text-sm">Nothing was left in AWS.</p>;
  }
  return (
    <div className="space-y-3">
      <p className="text-xs leading-5 text-muted-foreground">
        These stay in your AWS account. Delete each one in the AWS Console.
      </p>
      <ul className="border border-border bg-card text-xs">
        {leftovers.map((leftover) => (
          <li
            key={`${leftover.kind}:${leftover.region}:${leftover.id}`}
            className="flex items-center gap-3 border-b border-border px-3 py-2.5 last:border-b-0"
          >
            <span className="w-16 shrink-0 text-muted-foreground">
              {leftover.kind === "stack" ? "Stack" : "Instance"}
            </span>
            <span className="min-w-0 flex-1">
              <span className="mono block truncate text-foreground">{leftover.id}</span>
              <span className="mt-0.5 block text-[11px] text-muted-foreground">
                {leftover.region}
              </span>
            </span>
            <Button asChild variant="outline" size="sm">
              <a href={leftover.console_url} target="_blank" rel="noopener">
                Open
                <ExternalLink />
              </a>
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ConnectionDetail({ label, value }: { label: string; value: ReactNode }) {
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

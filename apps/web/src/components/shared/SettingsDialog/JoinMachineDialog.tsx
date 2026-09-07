import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Check, Circle, Loader2, Server } from "lucide-react";

import { CliHint } from "@/components/shared/CliHint";
import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogBody,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { UnitMachine } from "@/lib/api/schemas";
import { createMachineJoinCommand, machineJoinStatusQueryOptions } from "@/lib/queries/compute";
import { cn } from "@/lib/utils";

export function JoinMachineDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open ? <JoinMachineFlow /> : null}
    </Dialog>
  );
}

function JoinMachineFlow() {
  const join = useMutation({
    mutationFn: () => createMachineJoinCommand(),
  });
  const status = useQuery(machineJoinStatusQueryOptions(join.data?.id));
  const expired = status.data?.status === "expired" || status.data?.status === "revoked";

  return (
    <DialogContent className="flex max-h-[min(46rem,calc(100svh-2rem))] max-w-2xl flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl">
      <DialogHeader className="shrink-0 border-b border-border px-5 py-4 pr-12">
        <DialogTitle className="flex items-center gap-2 text-base">
          <Server className="size-4 text-brand" />
          Join a machine
        </DialogTitle>
        <DialogDescription>Connect a Linux amd64 or arm64 host to your account.</DialogDescription>
      </DialogHeader>

      <DialogBody className="p-5">
        {status.error ? (
          <ApiErrorNotice
            title="Could not check the machine"
            error={status.error}
            onRetry={() => void status.refetch()}
            retrying={status.isFetching}
          />
        ) : null}
        {expired ? (
          <p role="status" className="mb-3 text-sm text-muted-foreground">
            This install command {status.data?.status === "expired" ? "expired" : "was revoked"}.
            Generate another command to continue.
          </p>
        ) : null}
        {!join.data || expired ? (
          <GenerateCommandStep
            onGenerate={() => join.mutate()}
            pending={join.isPending}
            error={join.error}
          />
        ) : (
          <JoinProgress
            command={join.data.command}
            expiresAt={join.data.expires_at}
            machine={status.data?.machine ?? undefined}
          />
        )}
      </DialogBody>
    </DialogContent>
  );
}

function GenerateCommandStep({
  onGenerate,
  pending,
  error,
}: {
  onGenerate: () => void;
  pending: boolean;
  error: Error | null;
}) {
  return (
    <section aria-labelledby="generate-command-title">
      <h3 id="generate-command-title" className="text-sm font-medium">
        Prepare the host
      </h3>
      <p className="mt-1 text-xs text-muted-foreground">
        Generate a short-lived install command for one Linux machine.
      </p>
      <div className="mt-4 border border-border bg-muted/20 p-3 text-xs">
        <p className="font-medium">Host requirements</p>
        <ul className="mt-2 list-disc space-y-1.5 pl-4 text-muted-foreground">
          <li>Linux amd64 or arm64 with systemd and root or sudo access</li>
          <li>
            A rootful Docker daemon and WireGuard tools. WireGuard auto-install requires a supported
            apt-get or dnf host.
          </li>
          <li>Outbound DNS, HTTPS, and UDP port 51820 with stateful return traffic</li>
          <li>
            No route overlapping <code className="mono text-foreground">100.96.0.0/11</code>
          </li>
          <li>
            Host firewall access on <code className="mono text-foreground">wg-lazycloud</code> from{" "}
            <code className="mono text-foreground">100.96.0.0/24</code> to TCP port{" "}
            <code className="mono text-foreground">29443</code>
          </li>
        </ul>
        <p className="mt-2 text-muted-foreground">
          You do not need a public inbound port, port forwarding, or a static IP.
        </p>
      </div>
      <div className="mt-4">
        <Button onClick={onGenerate} pending={pending}>
          Generate install command
        </Button>
      </div>
      <div className="mt-5 border-l-2 border-warning bg-warning/5 px-3 py-2 text-xs text-muted-foreground">
        Run this command only on the machine you want to connect. Closing the dialog clears it from
        this browser.
      </div>
      {error ? <p className="mt-2 text-xs text-destructive">{error.message}</p> : null}
    </section>
  );
}

function JoinProgress({
  command,
  expiresAt,
  machine,
}: {
  command: string;
  expiresAt: string;
  machine: UnitMachine | undefined;
}) {
  const ready = machine?.readiness_phase === "ready";
  const blocked = machine?.readiness_phase === "blocked";
  const failedChecks = machine?.preflight_checks.filter((check) => !check.ok) ?? [];
  const checkRemediations = new Set(failedChecks.map((check) => check.remediation).filter(Boolean));
  const remediation = new Set(
    machine?.remediation.filter((item) => item && !checkRemediations.has(item)) ?? [],
  );

  return (
    <div className="space-y-5">
      <section aria-labelledby="install-command-title">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 id="install-command-title" className="text-sm font-medium">
              Run on the host
            </h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Credential expires <LiveRelativeTime value={expiresAt} />
            </p>
          </div>
          <StatusChip status={ready ? "Ready" : blocked ? "Failed" : "Pending"} />
        </div>
        <CliHint command={command} className="mt-3 bg-muted/30" />
        <p className="mt-2 text-[11px] text-muted-foreground">
          This command includes a temporary credential and appears only in this dialog.
        </p>
      </section>

      <section aria-labelledby="readiness-title" className="border-t border-border pt-4">
        <h3 id="readiness-title" className="text-sm font-medium">
          Readiness
        </h3>
        <div className="mt-3 grid gap-0 border border-border">
          <ProgressRow state="complete" label="Install command generated" />
          <ProgressRow
            state={machine ? "complete" : "active"}
            label={machine ? "Agent connected" : "Waiting for the agent to connect"}
          />
          <ProgressRow
            state={ready ? "complete" : blocked ? "blocked" : machine ? "active" : "pending"}
            label={
              ready
                ? "Host checks passed and capacity is schedulable"
                : blocked
                  ? "Host checks require attention"
                  : machine?.readiness_message || "Waiting for host checks"
            }
            last
          />
        </div>
      </section>

      {blocked ? (
        <section
          aria-labelledby="remediation-title"
          className="border-l-2 border-destructive bg-destructive/[0.035] p-3"
        >
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
            <div className="min-w-0 flex-1">
              <h3 id="remediation-title" className="text-sm font-medium">
                Fix host prerequisites
              </h3>
              {failedChecks.map((check) => (
                <div key={check.name} className="mt-3 text-xs">
                  <p className="font-medium">{check.name}</p>
                  <p className="mt-0.5 text-muted-foreground">{check.message}</p>
                  {check.remediation ? <p className="mt-1">{check.remediation}</p> : null}
                </div>
              ))}
              {remediation.size > 0 ? (
                <ul className="mt-3 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
                  {[...remediation].map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function ProgressRow({
  state,
  label,
  last = false,
}: {
  state: "complete" | "active" | "pending" | "blocked";
  label: string;
  last?: boolean;
}) {
  return (
    <div
      className={cn("grid grid-cols-[2.25rem_minmax(0,1fr)]", !last && "border-b border-border")}
    >
      <div className="flex items-center justify-center border-r border-border bg-muted/20">
        {state === "complete" ? (
          <Check className="size-4 text-positive" />
        ) : state === "active" ? (
          <Loader2 className="size-4 animate-spin text-brand" />
        ) : state === "blocked" ? (
          <AlertTriangle className="size-4 text-destructive" />
        ) : (
          <Circle className="size-3 text-muted-foreground/50" />
        )}
      </div>
      <p className={cn("px-3 py-2.5 text-xs", state === "pending" && "text-muted-foreground")}>
        {label}
      </p>
    </div>
  );
}

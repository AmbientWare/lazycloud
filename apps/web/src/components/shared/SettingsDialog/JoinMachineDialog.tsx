import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Check, Circle, Loader2, Server } from "lucide-react";

import { CliHint } from "@/components/shared/CliHint";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { UnitMachine } from "@/lib/api/schemas";
import {
  createMachineJoinCommand,
  machinesQueryOptions,
  type MachineJoinCommandInput,
} from "@/lib/queries/compute";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

import { WorkspaceChecklist } from "./MachineWorkspaces";

/** Mirrors the name rule on the join-command contract. */
const MACHINE_NAME_PATTERN = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;

function machineNameError(name: string): string | null {
  if (name.length === 0) return "Enter a name.";
  if (name.length > 63) return "Use at most 63 characters.";
  if (!MACHINE_NAME_PATTERN.test(name)) {
    return "Use lowercase letters, digits, and hyphens, starting and ending with a letter or digit.";
  }
  return null;
}

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
  const machinesQuery = useQuery(machinesQueryOptions());
  const [generatedAt, setGeneratedAt] = useState<number | null>(null);
  const [baselineMachineIds, setBaselineMachineIds] = useState<ReadonlySet<string>>(new Set());
  const join = useMutation({
    mutationFn: (input: MachineJoinCommandInput) => createMachineJoinCommand(input),
    onMutate: () => {
      setGeneratedAt(Date.now());
      setBaselineMachineIds(new Set((machinesQuery.data?.data ?? []).map((machine) => machine.id)));
    },
  });
  const targetMachine = findJoinedMachine(
    machinesQuery.data?.data ?? [],
    baselineMachineIds,
    generatedAt,
  );
  const queryError = machinesQuery.error;

  return (
    <DialogContent
      aria-describedby={undefined}
      className="flex max-h-[min(46rem,calc(100svh-2rem))] max-w-2xl flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl"
    >
      <DialogHeader className="shrink-0 border-b border-border px-5 py-4 pr-12">
        <DialogTitle className="flex items-center gap-2 text-base">
          <Server className="size-4 text-brand" />
          Join a machine
        </DialogTitle>
      </DialogHeader>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {machinesQuery.isPending ? (
          <div className="flex h-36 items-center justify-center text-sm text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />
            Loading connected machines
          </div>
        ) : queryError ? (
          <div className="border-l-2 border-destructive bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {queryError.message}
          </div>
        ) : !join.data ? (
          <GenerateCommandStep
            onGenerate={(input) => join.mutate(input)}
            pending={join.isPending}
            error={join.error}
          />
        ) : (
          <JoinProgress
            command={join.data.command}
            expiresAt={join.data.expires_at}
            machine={targetMachine}
          />
        )}
      </div>
    </DialogContent>
  );
}

function GenerateCommandStep({
  onGenerate,
  pending,
  error,
}: {
  onGenerate: (input: MachineJoinCommandInput) => void;
  pending: boolean;
  error: Error | null;
}) {
  const { workspace, workspaces } = useWorkspace();
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set([workspace.name]));
  const nameError = machineNameError(name);
  const canGenerate = nameError === null && selected.size > 0 && !pending;

  return (
    <form
      aria-labelledby="generate-command-title"
      onSubmit={(event) => {
        event.preventDefault();
        setNameTouched(true);
        if (!canGenerate) return;
        onGenerate({ name, workspaces: [...selected] });
      }}
    >
      <h3 id="generate-command-title" className="text-sm font-medium">
        Prepare the host
      </h3>
      <div className="space-y-4">
        <label className="block text-xs font-medium text-muted-foreground">
          Machine name
          <Input
            autoFocus
            value={name}
            disabled={pending}
            onChange={(event) => setName(event.target.value)}
            onBlur={() => setNameTouched(true)}
            placeholder="gpu-1"
            autoComplete="off"
            className="mono mt-1"
            aria-invalid={nameTouched && nameError !== null}
          />
          <span className="mt-1 block font-normal">
            {nameTouched && nameError ? nameError : 'Workloads select it with machine="gpu-1".'}
          </span>
        </label>
        <fieldset disabled={pending}>
          <legend className="mb-1.5 text-xs font-medium text-muted-foreground">Workspaces</legend>
          <WorkspaceChecklist workspaces={workspaces} selected={selected} onChange={setSelected} />
          <p className="mt-1 text-xs text-muted-foreground">
            {selected.size === 0
              ? "Select at least one workspace."
              : "Only workloads from these workspaces run here."}
          </p>
        </fieldset>
      </div>
      <div className="mt-4 border border-border bg-muted/20 p-3 text-xs">
        <p className="font-medium">Host requirements</p>
        <ul className="mt-2 list-disc space-y-1.5 pl-4 text-muted-foreground">
          <li>Linux amd64 or arm64 with systemd and root or sudo access</li>
          <li>A rootful Docker daemon</li>
          <li>Outbound DNS and TCP port 443 for HTTPS and authenticated TLS connections</li>
        </ul>
        <p className="mt-2 text-muted-foreground">
          You do not need a public inbound port, port forwarding, or a static IP.
        </p>
      </div>
      <div className="mt-4">
        <Button type="submit" disabled={!canGenerate}>
          {pending ? <Loader2 className="animate-spin" /> : null}
          Generate install command
        </Button>
      </div>
      {error ? <p className="mt-2 text-xs text-destructive">{error.message}</p> : null}
    </form>
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
  const joined = machine !== undefined && machine.lifecycle !== "requested";
  const ready = machine?.lifecycle === "ready";
  const blocked = machine?.lifecycle === "failed";
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
          <StatusChip status={ready ? "Ready" : blocked ? "Failed" : "Pending"} live={ready} />
        </div>
        <CliHint command={command} className="mt-3 bg-muted/30" />
        <p className="mt-2 text-[11px] text-muted-foreground">
          Run it only on the machine you want to connect. It carries a temporary credential and is
          cleared from this browser when you close the dialog.
        </p>
      </section>

      <section aria-labelledby="readiness-title" className="border-t border-border pt-4">
        <h3 id="readiness-title" className="text-sm font-medium">
          Connection status
        </h3>
        <div className="mt-3 grid gap-0 border border-border">
          <ProgressRow state="complete" label="Install command generated" />
          <ProgressRow
            state={joined ? "complete" : "active"}
            label={joined ? "Agent connected" : "Waiting for the agent to connect"}
          />
          <ProgressRow
            state={ready ? "complete" : blocked ? "blocked" : joined ? "active" : "pending"}
            label={
              ready
                ? "Machine is ready to run workloads"
                : blocked
                  ? "Host checks failed"
                  : (joined && machine?.lifecycle_message) || "Waiting for host checks"
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
                Resolve failed checks
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

function findJoinedMachine(
  machines: UnitMachine[],
  baselineMachineIds: ReadonlySet<string>,
  generatedAt: number | null,
): UnitMachine | undefined {
  if (generatedAt === null) return undefined;
  return machines
    .filter((machine) => {
      if (!baselineMachineIds.has(machine.id)) return true;
      if (!machine.last_seen_at) return false;
      return new Date(machine.last_seen_at).getTime() >= generatedAt;
    })
    .sort((left, right) => {
      const leftSeen = new Date(left.last_seen_at ?? 0).getTime();
      const rightSeen = new Date(right.last_seen_at ?? 0).getTime();
      return rightSeen - leftSeen;
    })[0];
}

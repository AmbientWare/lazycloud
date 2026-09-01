import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Loader2, Plus, Trash2 } from "lucide-react";

import { useCopyToClipboard } from "@/components/shared/CopyButton/useCopyToClipboard";
import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import type { CustomDomain } from "@/lib/api/schemas";
import {
  customDomainsQueryOptions,
  registerCustomDomain,
  removeCustomDomain,
} from "@/lib/queries/domains";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

export function DomainSettings({ onUpgrade }: { onUpgrade: () => void }) {
  const queryClient = useQueryClient();
  const [hostname, setHostname] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const domains = useQuery(customDomainsQueryOptions());
  const billing = useQuery(billingSummaryQueryOptions());

  const invalidate = () => queryClient.invalidateQueries({ queryKey: accountQueryKeys.domains() });

  const register = useMutation({
    mutationFn: (value: string) => registerCustomDomain(value),
    onSuccess: () => {
      setHostname("");
      setFailure(null);
      void invalidate();
    },
    onError: (error: Error) => setFailure(error.message),
  });

  const remove = useMutation({
    mutationFn: (value: string) => removeCustomDomain(value),
    onSuccess: () => {
      setFailure(null);
      void invalidate();
    },
    onError: (error: Error) => setFailure(error.message),
  });

  const pending = register.isPending || remove.isPending;
  const rows = domains.data?.data ?? [];
  const customDomainsEnabled = billing.data?.entitlements?.custom_domains ?? false;

  return (
    <Panel
      title="Domains"
      description="Registered for your account; any workspace in it can serve from them"
      action={
        billing.isPending || billing.error ? null : customDomainsEnabled ? (
          <form
            className="flex items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (hostname.trim()) register.mutate(hostname);
            }}
          >
            <Input
              value={hostname}
              onChange={(event) => setHostname(event.target.value)}
              placeholder="app.acme.com"
              aria-label="Domain to register"
              className="h-8 w-56"
              disabled={pending}
            />
            <Button size="sm" type="submit" disabled={pending || !hostname.trim()}>
              {register.isPending ? <Loader2 className="animate-spin" /> : <Plus />}
              Add
            </Button>
          </form>
        ) : (
          <Button size="sm" onClick={onUpgrade} disabled={billing.isPending}>
            Upgrade to Team
          </Button>
        )
      }
    >
      {billing.error || domains.error ? (
        <p className="border-b border-border/80 px-4 py-2 text-sm text-destructive" role="alert">
          {(billing.error ?? domains.error)?.message}
        </p>
      ) : failure ? (
        <p className="border-b border-border/80 px-4 py-2 text-[11px] text-destructive">
          {failure}
        </p>
      ) : null}
      {domains.isPending || billing.isPending ? (
        <div className="space-y-2 p-4">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : rows.length === 0 ? (
        <p className="p-4 text-[11px] text-muted-foreground">
          {customDomainsEnabled
            ? "No domains yet. Add one here to serve a deployment from a name you own."
            : "Custom domains are available on the Team plan. Every deployment still has a platform hostname."}
        </p>
      ) : (
        <ul className="divide-y divide-border/80">
          {rows.map((domain) => (
            <DomainRow
              key={domain.id}
              domain={domain}
              disabled={pending}
              onRemove={() => remove.mutate(domain.hostname)}
            />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function DomainRow({
  domain,
  disabled,
  onRemove,
}: {
  domain: CustomDomain;
  disabled: boolean;
  onRemove: () => void;
}) {
  return (
    <li className="flex items-start justify-between gap-3 px-4 py-2.5">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm text-foreground">{domain.hostname}</span>
          <StatusChip status={domain.phase} />
        </div>
        {/* The one thing the customer has to act on, so it is shown until it stops
            mattering, and shown as the record they have to create rather than as prose
            they would have to translate into one. */}
        {domain.phase !== "ready" && domain.cname_target ? (
          <DnsInstructions domain={domain} />
        ) : null}
        {domain.error_message ? (
          <p className="mt-0.5 text-[11px] text-destructive">{domain.error_message}</p>
        ) : null}
      </div>
      <Button
        variant="ghost"
        size="sm"
        aria-label={`Remove ${domain.hostname}`}
        disabled={disabled}
        onClick={onRemove}
      >
        <Trash2 />
      </Button>
    </li>
  );
}

/** The record to create, laid out the way a DNS form asks for it. */
function DnsInstructions({ domain }: { domain: CustomDomain }) {
  return (
    <div className="mt-1.5 space-y-1.5">
      <p className="text-[11px] text-muted-foreground">
        Add this record where you manage DNS for this domain. It can take a few minutes to take
        effect; nothing else is needed here.
      </p>
      <dl className="grid grid-cols-[3.5rem_minmax(0,1fr)] items-center gap-x-2 gap-y-1">
        <dt className="text-[11px] text-muted-foreground">Type</dt>
        <dd className="mono text-[11px] text-foreground">CNAME</dd>
        <dt className="text-[11px] text-muted-foreground">Name</dt>
        <dd>
          <CopyValue value={domain.hostname} label="record name" />
        </dd>
        <dt className="text-[11px] text-muted-foreground">Target</dt>
        <dd>
          <CopyValue value={domain.cname_target} label="record target" />
        </dd>
      </dl>
      {domain.required_records.length > 0 ? (
        <div className="space-y-1">
          <p className="text-[11px] text-muted-foreground">
            {domain.required_records.length === 1
              ? "Add this record too — it proves you own the domain, so a certificate can be issued for it:"
              : "Add these records too — they prove you own the domain, so a certificate can be issued for it:"}
          </p>
          {domain.required_records.map((record) => (
            <dl
              key={`${record.type}:${record.name}:${record.value}`}
              className="grid grid-cols-[3.5rem_minmax(0,1fr)] items-center gap-x-2 gap-y-1"
            >
              <dt className="text-[11px] text-muted-foreground">Type</dt>
              <dd className="mono text-[11px] text-foreground">{record.type}</dd>
              <dt className="text-[11px] text-muted-foreground">Name</dt>
              <dd>
                <CopyValue value={record.name} label="record name" />
              </dd>
              <dt className="text-[11px] text-muted-foreground">Value</dt>
              <dd>
                <CopyValue value={record.value} label="record value" />
              </dd>
            </dl>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function CopyValue({ value, label }: { value: string; label: string }) {
  const { copied, copy } = useCopyToClipboard(value);
  return (
    <button
      type="button"
      aria-label={`Copy ${label}`}
      title={copied ? "Copied" : `Copy ${label}`}
      onClick={copy}
      className="mono inline-flex max-w-full items-center gap-1.5 rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-foreground hover:bg-muted"
    >
      <span className="truncate">{value}</span>
      {copied ? (
        <Check className="size-3 shrink-0 text-positive" />
      ) : (
        <Copy className="size-3 shrink-0 opacity-60" />
      )}
    </button>
  );
}

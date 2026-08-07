import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, Trash2 } from "lucide-react";

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

export function DomainSettings({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const [hostname, setHostname] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const domains = useQuery(customDomainsQueryOptions(workspaceId));

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["custom-domains", workspaceId] });

  const register = useMutation({
    mutationFn: (value: string) => registerCustomDomain(workspaceId, value),
    onSuccess: () => {
      setHostname("");
      setFailure(null);
      void invalidate();
    },
    onError: (error: Error) => setFailure(error.message),
  });

  const remove = useMutation({
    mutationFn: (value: string) => removeCustomDomain(workspaceId, value),
    onSuccess: () => {
      setFailure(null);
      void invalidate();
    },
    onError: (error: Error) => setFailure(error.message),
  });

  const pending = register.isPending || remove.isPending;
  const rows = domains.data?.data ?? [];

  return (
    <Panel
      title="Domains"
      description="Domains this workspace can serve deployments from"
      action={
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
            placeholder="acme.com or *.acme.com"
            aria-label="Domain to register"
            className="h-8 w-56"
            disabled={pending}
          />
          <Button size="sm" type="submit" disabled={pending || !hostname.trim()}>
            {register.isPending ? <Loader2 className="animate-spin" /> : <Plus />}
            Add
          </Button>
        </form>
      }
    >
      {failure ? (
        <p className="border-b border-border/80 px-4 py-2 text-[11px] text-destructive">
          {failure}
        </p>
      ) : null}
      {domains.isPending ? (
        <div className="space-y-2 p-4">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : rows.length === 0 ? (
        <p className="p-4 text-[11px] text-muted-foreground">
          No domains yet. Every deployment already answers on a {""}
          <code>lazycloud.dev</code> hostname; add a domain here to serve one of your own.
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
        {/* The one thing the customer has to act on, so it is shown until it stops mattering. */}
        {domain.verification_target && domain.phase !== "ready" ? (
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            Add a CNAME pointing at <code>{domain.verification_target}</code>
          </p>
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

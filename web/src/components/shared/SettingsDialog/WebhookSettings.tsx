import { useState, type ReactNode } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Loader2, Plus, Send, Trash2 } from "lucide-react";

import { ConfirmButton } from "@/components/shared/ConfirmButton";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { Panel } from "@/components/shared/Panel";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import type { Schemas } from "@/lib/api/client";
import { webhookEventValues } from "@/lib/api/generated/openapi";
import { DOCS_URL } from "@/lib/env";
import { selectInfiniteList } from "@/lib/queries/infinite-list";
import {
  createWebhook,
  deleteWebhook,
  testWebhook,
  updateWebhook,
  webhookDeliveriesQueryOptions,
  webhooksQueryOptions,
} from "@/lib/queries/webhooks";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

type Webhook = Schemas["Webhook"];
type WebhookEvent = Schemas["WebhookEvent"];

export function WebhookSettings() {
  const { workspace } = useWorkspace();
  const webhooks = useInfiniteQuery(webhooksQueryOptions(workspace.name));
  const [creating, setCreating] = useState(false);
  const list = selectInfiniteList(webhooks.data, webhooks.hasNextPage, (webhook) => webhook.id);

  return (
    <Panel
      pending={webhooks.isPending}
      title="Webhooks"
      className="min-h-0 flex-1"
      headerClassName="flex-wrap"
      contentClassName="flex flex-col overflow-hidden"
      action={
        <Button size="sm" onClick={() => setCreating(true)} disabled={creating}>
          <Plus />
          Add webhook
        </Button>
      }
    >
      <p className="shrink-0 border-b border-border/80 px-4 py-2 text-[11px] text-muted-foreground">
        Deliveries are signed with the workspace secret{" "}
        <code className="mono">LAZYCLOUD_CALLBACK_SIGNING_KEY</code> in{" "}
        <code className="mono">X-Webhook-Signature</code> and{" "}
        <code className="mono">X-Webhook-Timestamp</code>.{" "}
        <a
          href={new URL("/platform/webhooks", DOCS_URL).href}
          target="_blank"
          rel="noreferrer"
          className="text-foreground underline underline-offset-2 hover:text-brand"
        >
          Verifying deliveries
        </a>
      </p>
      {creating ? (
        <CreateWebhookForm workspace={workspace.name} onDone={() => setCreating(false)} />
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {webhooks.isPending ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : webhooks.error && !webhooks.isFetchNextPageError ? (
          <PanelError message={webhooks.error.message} />
        ) : list.items.length === 0 ? (
          <PanelEmpty
            message="No webhooks yet"
            detail="Add one to receive task and deployment events."
            className="min-h-full p-4"
          />
        ) : (
          <>
            <ul aria-label="Webhooks" className="divide-y divide-border/80">
              {list.items.map((webhook) => (
                <WebhookRow key={webhook.id} workspace={workspace.name} webhook={webhook} />
              ))}
            </ul>
            <InfiniteScrollBoundary
              nextCursor={list.nextCursor}
              loading={webhooks.isFetchingNextPage}
              error={webhooks.isFetchNextPageError}
              onLoadMore={() => void webhooks.fetchNextPage()}
              resourceLabel="webhooks"
            />
          </>
        )}
      </div>
    </Panel>
  );
}

function CreateWebhookForm({ workspace, onDone }: { workspace: string; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<ReadonlySet<WebhookEvent>>(new Set());
  const [app, setApp] = useState("");
  const [description, setDescription] = useState("");

  const create = useMutation({
    mutationFn: () =>
      createWebhook(workspace, {
        url: url.trim(),
        events: webhookEventValues.filter((event) => events.has(event)),
        app: app.trim() || undefined,
        description: description.trim() || undefined,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.webhooks.list(workspace) });
      onDone();
    },
  });

  return (
    <form
      aria-label="New webhook"
      className="grid shrink-0 gap-3 border-b border-border p-4 sm:grid-cols-2"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate();
      }}
    >
      <label className="grid gap-1.5 sm:col-span-2">
        <span className="micro-label">URL</span>
        <Input
          autoFocus
          value={url}
          disabled={create.isPending}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://example.com/hooks/lazycloud"
          className="h-8 bg-background"
        />
      </label>
      <fieldset className="grid gap-1.5 sm:col-span-2">
        <legend className="micro-label mb-1.5">Events</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {webhookEventValues.map((event) => (
            <label
              key={event}
              className="flex cursor-pointer items-center gap-2 text-xs has-[:disabled]:cursor-default"
            >
              <Checkbox
                checked={events.has(event)}
                disabled={create.isPending}
                onCheckedChange={(value) => {
                  const next = new Set(events);
                  if (value === true) next.add(event);
                  else next.delete(event);
                  setEvents(next);
                }}
              />
              <span className="mono">{event}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <label className="grid gap-1.5">
        <span className="micro-label">App</span>
        <Input
          value={app}
          disabled={create.isPending}
          onChange={(event) => setApp(event.target.value)}
          placeholder="All apps"
          className="h-8 bg-background"
        />
      </label>
      <label className="grid gap-1.5">
        <span className="micro-label">Description</span>
        <Input
          value={description}
          disabled={create.isPending}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="Optional"
          className="h-8 bg-background"
        />
      </label>
      <div className="flex items-center justify-end gap-2 sm:col-span-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={create.isPending}
          onClick={onDone}
        >
          Cancel
        </Button>
        <Button
          type="submit"
          size="sm"
          disabled={create.isPending || !url.trim() || events.size === 0}
        >
          {create.isPending ? <Loader2 className="animate-spin" /> : null}
          Add webhook
        </Button>
      </div>
      {create.error ? (
        <p className="text-xs text-destructive sm:col-span-2" role="alert">
          {create.error.message}
        </p>
      ) : null}
    </form>
  );
}

function WebhookRow({ workspace, webhook }: { workspace: string; webhook: Webhook }) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const refreshList = () =>
    queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.webhooks.list(workspace) });

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => updateWebhook(workspace, webhook.id, { enabled }),
    onSuccess: () => void refreshList(),
  });
  const test = useMutation({
    mutationFn: () => testWebhook(workspace, webhook.id),
    onSuccess: () => {
      setExpanded(true);
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.webhooks.deliveries(workspace, webhook.id),
      });
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteWebhook(workspace, webhook.id),
    onSuccess: () => void refreshList(),
  });

  const busy = toggle.isPending || test.isPending || remove.isPending;
  const failure = toggle.error ?? test.error ?? remove.error;
  const enabled = toggle.isPending ? (toggle.variables ?? webhook.enabled) : webhook.enabled;

  return (
    <li>
      <div className="flex items-start justify-between gap-3 px-4 py-2.5">
        <button
          type="button"
          aria-expanded={expanded}
          aria-label={`${expanded ? "Hide" : "Show"} deliveries to ${webhook.url}`}
          onClick={() => setExpanded((value) => !value)}
          className="flex min-w-0 flex-1 items-start gap-2 text-left"
        >
          <ChevronRight
            className={cn(
              "mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none",
              expanded && "rotate-90",
            )}
            aria-hidden="true"
          />
          <span className="min-w-0">
            <span className="mono block truncate text-sm text-foreground">{webhook.url}</span>
            <span className="mt-0.5 block truncate text-[11px] text-muted-foreground">
              {webhook.app ?? "All apps"} / {webhook.events.join(", ")}
            </span>
            {webhook.description ? (
              <span className="mt-0.5 block truncate text-[11px] text-muted-foreground">
                {webhook.description}
              </span>
            ) : null}
          </span>
        </button>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <div className="flex items-center gap-2">
            <Switch
              checked={enabled}
              disabled={busy}
              onCheckedChange={(next) => toggle.mutate(next)}
              aria-label={`Enable ${webhook.url}`}
            />
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => test.mutate()}>
              {test.isPending ? <Loader2 className="animate-spin" /> : <Send />}
              Send test
            </Button>
            <ConfirmButton
              label={`Delete webhook to ${webhook.url}`}
              icon={Trash2}
              disabled={busy}
              pending={remove.isPending}
              onConfirm={() => remove.mutate()}
            />
          </div>
          {failure ? (
            <p className="text-right text-xs text-destructive" role="alert">
              {failure.message}
            </p>
          ) : null}
        </div>
      </div>
      {expanded ? <WebhookDeliveries workspace={workspace} webhook={webhook.id} /> : null}
    </li>
  );
}

function WebhookDeliveries({ workspace, webhook }: { workspace: string; webhook: string }) {
  const deliveries = useQuery(webhookDeliveriesQueryOptions(workspace, webhook));

  return (
    <div className="border-t border-border/80 bg-muted/20 pb-2">
      {deliveries.isPending ? (
        <div className="space-y-2 px-4 py-3">
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-6 w-full" />
        </div>
      ) : deliveries.error ? (
        <PanelError message={deliveries.error.message} />
      ) : deliveries.data.deliveries.length === 0 ? (
        <PanelEmpty message="No deliveries in the last 7 days" className="p-4" />
      ) : (
        <div>
          <div
            className="micro-label hidden h-8 grid-cols-12 items-center gap-3 px-4 lg:grid"
            aria-hidden="true"
          >
            <span className="col-span-2">Time</span>
            <span className="col-span-2">Event</span>
            <span className="col-span-2">State</span>
            <span className="col-span-1">Attempts</span>
            <span className="col-span-1">Response</span>
            <span className="col-span-4">Last error</span>
          </div>
          <ul aria-label="Deliveries">
            {deliveries.data.deliveries.map((delivery) => (
              <DeliveryRow key={delivery.id} delivery={delivery} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function DeliveryRow({ delivery }: { delivery: Schemas["WebhookDelivery"] }) {
  return (
    <li className="grid grid-cols-2 gap-x-6 gap-y-2 px-4 py-2 text-xs lg:grid-cols-12 lg:items-center lg:gap-3 lg:py-1.5">
      <DeliveryCell label="Time" className="text-muted-foreground lg:col-span-2">
        <LiveRelativeTime value={delivery.created_at} />
      </DeliveryCell>
      <DeliveryCell label="Event" className="lg:col-span-2">
        <span className="mono block truncate">{delivery.event}</span>
      </DeliveryCell>
      <DeliveryCell label="State" className="lg:col-span-2">
        <StatusChip status={delivery.state} live={delivery.state === "pending"} />
      </DeliveryCell>
      <DeliveryCell label="Attempts" className="lg:col-span-1">
        <span className="mono">{delivery.attempts}</span>
      </DeliveryCell>
      <DeliveryCell label="Response" className="lg:col-span-1">
        <span className="mono">{delivery.response_status ?? "None"}</span>
      </DeliveryCell>
      <DeliveryCell
        label="Last error"
        className={cn(
          "col-span-2 break-words lg:col-span-4",
          delivery.last_error ? "text-destructive" : "text-muted-foreground",
        )}
      >
        {delivery.last_error ?? "None"}
      </DeliveryCell>
    </li>
  );
}

function DeliveryCell({
  label,
  className,
  children,
}: {
  label: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <div className="micro-label mb-1 lg:hidden">{label}</div>
      {children}
    </div>
  );
}

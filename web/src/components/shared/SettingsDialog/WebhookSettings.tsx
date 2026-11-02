import { useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query";
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
type WebhookDelivery = Schemas["WebhookDelivery"];
type Pages<T> = InfiniteData<{ data: T[]; next: string }, string>;

/** What each event means to the person choosing it; the receiver sees the code. */
const EVENTS: Record<WebhookEvent, { group: "Tasks" | "Deploys"; label: string }> = {
  "task.succeeded": { group: "Tasks", label: "Task succeeded" },
  "task.failed": { group: "Tasks", label: "Task failed after its retries" },
  "task.cancelled": { group: "Tasks", label: "Task cancelled" },
  "workload.deployed": { group: "Deploys", label: "New version live" },
  "workload.failed": { group: "Deploys", label: "Version stopped starting" },
};

const EVENT_GROUPS = (["Tasks", "Deploys"] as const).map((group) => ({
  group,
  events: webhookEventValues.filter((event) => EVENTS[event].group === group),
}));

function eventLabel(event: string): string {
  if (event === "webhook.test") return "Test";
  return event in EVENTS ? EVENTS[event as WebhookEvent].label : event;
}

/** Replace or drop one webhook in the cached pages, so a change shows without a reload. */
function patchWebhooks(
  queryClient: QueryClient,
  workspace: string,
  id: string,
  next: Webhook | null,
) {
  queryClient.setQueryData<Pages<Webhook>>(workspaceQueryKeys.webhooks.list(workspace), (data) =>
    data
      ? {
          ...data,
          pages: data.pages.map((page) => ({
            ...page,
            data: next
              ? page.data.map((webhook) => (webhook.id === id ? next : webhook))
              : page.data.filter((webhook) => webhook.id !== id),
          })),
        }
      : data,
  );
}

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
      <p className="shrink-0 border-b border-border/80 px-4 py-2 text-xs text-muted-foreground">
        Each request is signed with your workspace&apos;s signing key, so your server can check it
        came from LazyCloud.{" "}
        <a
          href={new URL("/platform/webhooks#verify-a-delivery", DOCS_URL).href}
          target="_blank"
          rel="noreferrer"
          className="text-foreground underline underline-offset-2 hover:text-brand"
        >
          How to verify a request
        </a>
      </p>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {creating ? (
          <CreateWebhookForm workspace={workspace.name} onDone={() => setCreating(false)} />
        ) : null}
        {webhooks.isPending ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : webhooks.error && !webhooks.isFetchNextPageError ? (
          <PanelError message={webhooks.error.message} />
        ) : list.items.length === 0 ? (
          creating ? null : (
            <PanelEmpty
              message="No webhooks yet"
              detail="Add a URL to get a request when a task fails or a new version goes live."
              className="min-h-full p-4"
            />
          )
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
      <fieldset className="grid gap-2 sm:col-span-2">
        <legend className="micro-label mb-1.5">Send a request when</legend>
        <div className="grid gap-3 sm:grid-cols-2">
          {EVENT_GROUPS.map(({ group, events: groupEvents }) => (
            <div key={group} className="grid content-start gap-1.5">
              <span className="text-xs font-medium text-foreground">{group}</span>
              {groupEvents.map((event) => (
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
                  <span>{EVENTS[event].label}</span>
                  <span className="mono text-[11px] text-muted-foreground">{event}</span>
                </label>
              ))}
            </div>
          ))}
        </div>
      </fieldset>
      <label className="grid gap-1.5">
        <span className="micro-label">Only for app</span>
        <Input
          value={app}
          disabled={create.isPending}
          onChange={(event) => setApp(event.target.value)}
          placeholder="Every app"
          className="h-8 bg-background"
        />
      </label>
      <label className="grid gap-1.5">
        <span className="micro-label">Note</span>
        <Input
          value={description}
          disabled={create.isPending}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="Optional, such as on-call alerts"
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
  const deliveriesKey = workspaceQueryKeys.webhooks.deliveries(workspace, webhook.id);

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => updateWebhook(workspace, webhook.id, { enabled }),
    onSuccess: (updated) => patchWebhooks(queryClient, workspace, webhook.id, updated),
  });
  const test = useMutation({
    mutationFn: () => testWebhook(workspace, webhook.id),
    onSuccess: (delivery) => {
      // The queued delivery shows at once; its pending state keeps the list polling.
      queryClient.setQueryData<Pages<WebhookDelivery>>(deliveriesKey, (data) =>
        data?.pages[0]
          ? {
              ...data,
              pages: [
                { ...data.pages[0], data: [delivery, ...data.pages[0].data] },
                ...data.pages.slice(1),
              ],
            }
          : data,
      );
      setExpanded(true);
      void queryClient.invalidateQueries({ queryKey: deliveriesKey });
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteWebhook(workspace, webhook.id),
    onSuccess: () => {
      patchWebhooks(queryClient, workspace, webhook.id, null);
      queryClient.removeQueries({ queryKey: deliveriesKey });
    },
  });

  const busy = toggle.isPending || test.isPending || remove.isPending;
  const failure = toggle.error ?? test.error ?? remove.error;
  const enabled = toggle.isPending ? (toggle.variables ?? webhook.enabled) : webhook.enabled;

  return (
    <li>
      <div className="flex flex-col gap-2 px-4 py-2.5 sm:flex-row sm:items-start sm:justify-between sm:gap-3">
        <button
          type="button"
          aria-expanded={expanded}
          aria-label={`${expanded ? "Hide" : "Show"} deliveries to ${webhook.url}`}
          onClick={() => setExpanded((value) => !value)}
          className="flex min-w-0 flex-1 items-start gap-2 rounded-sm text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          <ChevronRight
            className={cn(
              "mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none",
              expanded && "rotate-90",
            )}
            aria-hidden="true"
          />
          <span className="min-w-0">
            <span
              className={cn(
                "mono block truncate text-sm",
                enabled ? "text-foreground" : "text-muted-foreground",
              )}
            >
              {webhook.url}
            </span>
            <span className="mt-0.5 block text-xs text-muted-foreground">
              {enabled ? null : <span className="font-medium text-foreground">Paused. </span>}
              {webhook.app ? `Only ${webhook.app}` : "Every app"}:{" "}
              {webhook.events.map(eventLabel).join(", ").toLowerCase()}
            </span>
            {webhook.description ? (
              <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                {webhook.description}
              </span>
            ) : null}
          </span>
        </button>
        <div className="flex shrink-0 flex-col items-start gap-1 pl-6 sm:items-end sm:pl-0">
          <div className="flex items-center gap-2">
            <Switch
              checked={enabled}
              disabled={busy}
              onCheckedChange={(next) => toggle.mutate(next)}
              aria-label={`Send requests to ${webhook.url}`}
              title={enabled ? "Pause" : "Resume"}
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
            <p className="text-xs text-destructive sm:text-right" role="alert">
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
  const deliveries = useInfiniteQuery(webhookDeliveriesQueryOptions(workspace, webhook));
  const list = selectInfiniteList(
    deliveries.data,
    deliveries.hasNextPage,
    (delivery) => delivery.id,
  );

  return (
    <div className="border-t border-border/80 bg-muted/20 pb-2">
      {deliveries.isPending ? (
        <div className="space-y-2 px-4 py-3" aria-busy="true">
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-6 w-full" />
        </div>
      ) : deliveries.error && !deliveries.isFetchNextPageError ? (
        <PanelError message={deliveries.error.message} />
      ) : list.items.length === 0 ? (
        <PanelEmpty
          message="Nothing sent in the last 7 days"
          detail="Send a test to check the URL answers."
          className="p-4"
        />
      ) : (
        <div>
          <div
            className="micro-label hidden h-8 grid-cols-12 items-center gap-3 px-4 lg:grid"
            aria-hidden="true"
          >
            <span className="col-span-2">Sent</span>
            <span className="col-span-3">Event</span>
            <span className="col-span-2">Result</span>
            <span className="col-span-1">Tries</span>
            <span className="col-span-1">Status</span>
            <span className="col-span-3">Error</span>
          </div>
          <ul aria-label="Deliveries">
            {list.items.map((delivery) => (
              <DeliveryRow key={delivery.id} delivery={delivery} />
            ))}
          </ul>
          <InfiniteScrollBoundary
            nextCursor={list.nextCursor}
            loading={deliveries.isFetchingNextPage}
            error={deliveries.isFetchNextPageError}
            onLoadMore={() => void deliveries.fetchNextPage()}
            resourceLabel="deliveries"
          />
        </div>
      )}
    </div>
  );
}

function DeliveryRow({ delivery }: { delivery: WebhookDelivery }) {
  const status = delivery.response_status ? (
    <span className="mono">{delivery.response_status}</span>
  ) : (
    <span className="text-muted-foreground">No answer</span>
  );
  return (
    <li className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 px-4 py-2 text-xs lg:grid-cols-12 lg:gap-3 lg:py-1.5">
      <span className="truncate lg:col-span-3 lg:order-2" title={delivery.event}>
        {eventLabel(delivery.event)}
      </span>
      <span className="text-right text-muted-foreground lg:order-1 lg:col-span-2 lg:text-left">
        <LiveRelativeTime value={delivery.created_at} />
      </span>
      <span className="flex items-center gap-3 lg:order-3 lg:col-span-4 lg:grid lg:grid-cols-4 lg:gap-3">
        <span className="lg:col-span-2">
          <StatusChip status={delivery.state} live={delivery.state === "pending"} />
        </span>
        <span className="mono lg:col-span-1">
          {delivery.attempts}
          <span className="font-sans text-muted-foreground lg:hidden">
            {delivery.attempts === 1 ? " try" : " tries"}
          </span>
        </span>
        <span className="lg:col-span-1">{status}</span>
      </span>
      {delivery.last_error ? (
        <span className="col-span-2 break-words text-destructive lg:order-4 lg:col-span-3">
          {delivery.last_error}
        </span>
      ) : null}
    </li>
  );
}

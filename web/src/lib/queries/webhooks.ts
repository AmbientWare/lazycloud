import { infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { LIVE_LIST_MAX_PAGES, nextListCursor } from "./infinite-list";
import { workspaceQueryKeys } from "./workspace-keys";

/** How often an open delivery list rereads while a delivery is still being sent. */
const PENDING_DELIVERY_POLL_MS = 3_000;

const webhookPath = (workspace: string, webhook: string) => ({
  params: { path: { workspace, webhook } },
});

/** The workspace's webhooks, oldest first, a page at a time. */
export function webhooksQueryOptions(workspace: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.webhooks.list(workspace),
    initialPageParam: "",
    queryFn: async ({ pageParam }) => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/webhooks", {
          params: { path: { workspace }, query: { cursor: pageParam || undefined } },
        }),
      );
      return { data: page.webhooks, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
  });
}

/** The webhook's deliveries, newest first, a page at a time. The pages reread
 * while the newest page still has a delivery being sent. */
export function webhookDeliveriesQueryOptions(workspace: string, webhook: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.webhooks.deliveries(workspace, webhook),
    initialPageParam: "",
    queryFn: async ({ pageParam }) => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/webhooks/{webhook}/deliveries", {
          params: { path: { workspace, webhook }, query: { cursor: pageParam || undefined } },
        }),
      );
      return { data: page.deliveries, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
    refetchInterval: (query) =>
      query.state.data?.pages[0]?.data.some(({ state }) => state === "pending")
        ? PENDING_DELIVERY_POLL_MS
        : false,
  });
}

export function createWebhook(workspace: string, body: Schemas["WebhookCreate"]) {
  return ok(
    api.POST("/v1/workspaces/{workspace}/webhooks", { params: { path: { workspace } }, body }),
  );
}

export function updateWebhook(workspace: string, webhook: string, body: Schemas["WebhookUpdate"]) {
  return ok(
    api.PATCH("/v1/workspaces/{workspace}/webhooks/{webhook}", {
      ...webhookPath(workspace, webhook),
      body,
    }),
  );
}

export function testWebhook(workspace: string, webhook: string) {
  return ok(
    api.POST("/v1/workspaces/{workspace}/webhooks/{webhook}/test", webhookPath(workspace, webhook)),
  );
}

export function deleteWebhook(workspace: string, webhook: string) {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/webhooks/{webhook}", webhookPath(workspace, webhook)),
  );
}

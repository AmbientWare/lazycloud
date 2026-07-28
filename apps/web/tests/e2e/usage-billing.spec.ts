import { expect, test, type Page } from "@playwright/test";

import { usageBillingOverviewFixture, usageBillingWorkloadsFixture } from "./usage-fixtures";
import { activeWorkspace } from "./fixtures/workspaces";

const workspace = activeWorkspace("workspace-test", "acme");

test("current period is resolved by the backend and keeps detail and export on its snapshot", async ({
  page,
}) => {
  const requests = await mockUsageBilling(page);
  await page.goto("/w/acme/usage");

  await page.getByRole("link", { name: "Current period" }).click();
  await expect(page).toHaveURL(/range=current/);
  await expect(page.getByRole("heading", { name: "Cost summary" })).toBeVisible();
  const currentOverview = requests.overview
    .map((request) => new URL(request))
    .find((url) => url.searchParams.get("period") === "current");
  expect(currentOverview).toBeDefined();
  expect(currentOverview?.searchParams.has("start")).toBe(false);
  expect(currentOverview?.searchParams.has("end")).toBe(false);

  const apps = page.locator("section", {
    has: page.getByRole("heading", { name: "Apps", exact: true }),
  });
  await apps.getByRole("button", { name: /trainer.*2 tasks.*0\.53/ }).click();
  await expect.poll(() => requests.detail.length).toBe(1);
  const detailUrl = new URL(requests.detail[0] ?? "");
  expect(detailUrl.searchParams.get("period")).toBeNull();
  expect(detailUrl.searchParams.get("start")).toBe("2026-07-09T12:00:00Z");
  expect(detailUrl.searchParams.get("end")).toBe("2026-07-10T12:00:00Z");

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV" }).click();
  await downloadPromise;
  const exportUrl = new URL(requests.export[0]?.url ?? "");
  expect(exportUrl.searchParams.get("period")).toBeNull();
  expect(exportUrl.searchParams.get("start")).toBe("2026-07-09T12:00:00Z");
  expect(exportUrl.searchParams.get("end")).toBe("2026-07-10T12:00:00Z");
});

async function mockUsageBilling(page: Page) {
  const exportRequests: Array<{ url: string; authorization: string | undefined }> = [];
  const detailRequests: string[] = [];
  const overviewRequests: string[] = [];
  await page.addInitScript(() => {
    localStorage.setItem("lazycloud_web_token", "usage-token");

    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const requestUrl = input instanceof Request ? input.url : input.toString();
      const url = new URL(requestUrl, window.location.href);
      if (url.pathname !== "/api/v1/events/changes/stream") {
        return nativeFetch(input, init);
      }

      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(": connected\n\n"));
          },
        }),
        { headers: { "content-type": "text/event-stream" } },
      );
    };
  });
  await page.route("**/api/v1/workspaces?include_deleting=true", (route) =>
    route.fulfill({ json: { workspaces: [workspace] } }),
  );
  await page.route("**/api/v1/usage/billing/workloads?*", (route) => {
    detailRequests.push(route.request().url());
    return route.fulfill({ json: usageBillingWorkloadsFixture(workspace.id) });
  });
  await page.route("**/api/v1/usage/billing*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/billing.csv")) {
      const headers = await route.request().allHeaders();
      exportRequests.push({
        url: route.request().url(),
        authorization: headers["authorization"],
      });
      return route.fulfill({
        contentType: "text/csv",
        headers: {
          "content-disposition": 'attachment; filename="usage-2026-07-09-to-2026-07-10.csv"',
        },
        body: "section,metric,cost_nanos\nsummary,cpu_seconds,36000000\n",
      });
    }
    overviewRequests.push(route.request().url());
    return route.fulfill({ json: usageBillingOverviewFixture(workspace.id) });
  });
  await page.route("**/api/v1/concurrency-limits*", (route) =>
    route.fulfill({
      json: {
        limits: [
          {
            id: "limit-1",
            workspace_id: workspace.id,
            name: "workspace",
            limit: 20,
            in_flight: 8,
            resource_type: "workspace",
            resource_id: null,
            available: 12,
            saturated: false,
            created_at: "2026-07-01T00:00:00Z",
            updated_at: "2026-07-10T12:00:00Z",
          },
        ],
      },
    }),
  );
  await page.route("**/api/v1/workers*", (route) =>
    route.fulfill({ status: 403, json: { detail: "admin only" } }),
  );
  return { export: exportRequests, detail: detailRequests, overview: overviewRequests };
}

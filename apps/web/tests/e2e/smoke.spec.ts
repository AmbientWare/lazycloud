import { expect, test, type Page } from "@playwright/test";

import { emptyUsageBillingOverviewFixture } from "./usage-fixtures";
import { activeWorkspaceDefaults } from "./fixtures/workspaces";

const workspaceDefaults = activeWorkspaceDefaults;

async function mockControlPlane(page: Page) {
  const workspaces = [
    { id: "workspace-test", name: "acme", ...workspaceDefaults },
    { id: "workspace-second", name: "beta", ...workspaceDefaults },
  ];
  await page.addInitScript(() => {
    localStorage.setItem("lazycloud_web_token", "test-token");
  });
  await page.route("**/api/v1/workspaces?include_deleting=true", async (route) => {
    await route.fulfill({
      json: {
        workspaces,
      },
    });
  });
  await page.route("**/api/v1/concurrency-limits*", async (route) => {
    await route.fulfill({
      json: {
        limits: [
          {
            id: "limit-1",
            workspace_id: "workspace-test",
            name: "cpu",
            limit: 30,
            in_flight: 0,
            resource_type: "workspace",
            resource_id: null,
            available: 30,
            saturated: false,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          },
        ],
      },
    });
  });
  await page.route("**/api/v1/tasks/metrics*", async (route) => {
    await route.fulfill({
      json: {
        total: 4,
        status_counts: { running: 1, complete: 3 },
        completed: 3,
        failed: 0,
        cancelled: 0,
        average_runtime_ms: 1500,
        failure_rate: 0,
        runtime_ms_p50: 1200,
        runtime_ms_p95: 2400,
        runtime_ms_p99: 2800,
        startup_ms_p50: 600,
        startup_ms_p95: 900,
      },
    });
  });
  await page.route("**/api/v1/tasks/aggregate-by-time-window*", async (route) => {
    const currentHour = new Date();
    currentHour.setUTCMinutes(0, 0, 0);
    await route.fulfill({
      json: {
        items: [
          {
            timestamp: currentHour.toISOString(),
            count: 4,
            status_counts: { complete: 3, running: 1, failed: 1 },
          },
        ],
      },
    });
  });
  await page.route("**/api/v1/deployments*", async (route) => {
    await route.fulfill({ json: { data: [], next: "" } });
  });
  await page.route("**/api/v1/apps/summaries*", async (route) => {
    await route.fulfill({
      json: {
        items: [
          {
            app: {
              id: "app-1",
              workspace_id: "workspace-test",
              stub_id: "stub-1",
              name: "square_app",
              version: 1,
              public: false,
              active: true,
              created_at: "2026-01-01T09:00:00Z",
              updated_at: "2026-01-01T10:00:00Z",
            },
            latest_workload: {
              id: "stub-1",
              workspace_id: "workspace-test",
              name: "square",
              kind: "function",
              handler: "app:square",
              deployment_id: "deployment-1",
              app_id: "app-1",
              public: false,
              config: { runtime: null },
              created_at: "2026-01-01T09:00:00Z",
              updated_at: "2026-01-01T10:00:00Z",
            },
            latest_deployment: {
              id: "deployment-1",
              name: "square",
              kind: "function",
              app_id: "app-1",
              stub_id: "stub-1",
              version: 7,
              active: true,
              created_at: "2026-01-01T10:00:00Z",
              updated_at: "2026-01-01T10:00:00Z",
            },
            workload_kinds: { function: 1 },
            workload_count: 1,
            active_versions: 1,
            running_containers: 0,
            runs_24h: 4,
            failed_runs_24h: 1,
            activity_24h: [0, 1, 3],
            failures_24h: [0, 0, 1],
            last_deployed_at: "2026-01-01T10:00:00Z",
          },
        ],
      },
    });
  });
  await page.route("**/api/v1/containers*", async (route) => {
    await route.fulfill({ json: { data: [], next: "" } });
  });
  await page.route("**/api/v1/tokens*", async (route) => {
    await route.fulfill({ json: { tokens: [] } });
  });
  await page.route("**/api/v1/stubs?*", async (route) => {
    await route.fulfill({ json: { stubs: [] } });
  });
  await page.route("**/api/v1/stubs/sandboxes*", async (route) => {
    await route.fulfill({ json: { data: [], next: "" } });
  });
  await page.route("**/api/v1/logs?*", async (route) => {
    await route.fulfill({ json: { data: [], next: "", count: 0, total_expected: 0 } });
  });
  await page.route("**/api/v1/logs/stream*", async (route) => {
    await route.fulfill({
      contentType: "text/event-stream",
      body: ": connected\n\n",
    });
  });
  await page.route("**/api/v1/events/history*", async (route) => {
    await route.fulfill({ json: { data: [], next: "", count: 0 } });
  });
  await page.route("**/api/v1/events/changes/stream*", async (route) => {
    await route.fulfill({
      contentType: "text/event-stream",
      body: ": connected\n\n",
    });
  });
  await page.route("**/api/v1/tasks?*", async (route) => {
    await route.fulfill({ json: { data: [], next: "" } });
  });
  await page.route("**/api/v1/usage/billing*", async (route) => {
    await route.fulfill({ json: emptyUsageBillingOverviewFixture("workspace-test") });
  });
  // Non-admin token by default: the Compute nav entry stays hidden.
  await page.route("**/api/v1/workers*", async (route) => {
    await route.fulfill({ status: 403, json: { detail: "admin scope required" } });
  });
}

test("dashboard entry lands on Apps and the responsive shell switches workspaces", async ({ page }) => {
  await mockControlPlane(page);
  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/w\/acme\/apps\/?$/);

  const mobile = (page.viewportSize()?.width ?? 1280) < 1024;
  const nav = page.getByRole("navigation", {
    name: mobile ? "Mobile navigation" : "Main navigation",
  });
  await expect(nav.getByRole("link", { name: "Apps" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Tasks" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Storage" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Usage" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Home" })).toHaveCount(0);
  if (mobile) {
    await page.getByRole("button", { name: "Open workspace menu" }).click();
    const menu = page.getByRole("dialog");
    await expect(menu.getByRole("link", { name: "Settings" })).toBeVisible();
    await expect(menu.getByRole("link", { name: "Compute" })).toHaveCount(0);
    await page.getByRole("button", { name: "Close" }).click();
  } else {
    await expect(nav.getByRole("link", { name: "Apps" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const secondary = page.getByRole("navigation", { name: "Workspace navigation" });
    await expect(secondary.getByRole("link", { name: "Settings" })).toBeVisible();
    await expect(secondary.getByRole("link", { name: "Compute" })).toHaveCount(0);
  }

  await expect(page.getByRole("link", { name: /square_app/ })).toBeVisible();
  await expect(page.getByRole("contentinfo")).toHaveCount(0);
  await expect(page.getByText(/cpu concurrency/)).toHaveCount(0);

  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  await page.getByRole("menuitem", { name: "beta" }).click();
  await expect(page).toHaveURL(/\/w\/beta\/apps\/?$/);

  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/w\/beta\/apps\/?$/);
});

test("workspace search opens from the keyboard and navigates to a canonical resource URL", async ({
  page,
}) => {
  await mockControlPlane(page);
  await page.route("**/api/v1/stubs?*", async (route) => {
    await route.fulfill({
      json: {
        stubs: [
          {
            id: "stub-1",
            workspace_id: "workspace-test",
            name: "square",
            kind: "function",
            handler: "app:square",
            app_id: "app-1",
            public: false,
            config: { runtime: null },
            created_at: "2026-01-01T09:00:00Z",
            updated_at: "2026-01-01T10:00:00Z",
          },
        ],
      },
    });
  });
  await page.route("**/api/v1/tasks?*", async (route) => {
    await route.fulfill({
      json: {
        data: [
          {
            id: "task-1",
            name: "square",
            status: "complete",
            app_id: "app-1",
            stub_id: "stub-1",
            created_at: "2026-01-01T10:00:00Z",
          },
        ],
        next: "",
      },
    });
  });
  await page.goto("/w/acme/apps");
  await expect(page.getByRole("link", { name: /square_app/ })).toBeVisible();

  await page.keyboard.press("/");
  const search = page.getByRole("dialog");
  await expect(search.getByRole("combobox")).toBeFocused();
  await search.getByRole("combobox").fill("square");
  await expect(search.getByRole("option", { name: /square_app/ })).toBeVisible();
  await expect(search.getByText("app-1", { exact: true })).toHaveCount(0);
  await expect(search.getByText("stub-1", { exact: true })).toHaveCount(0);
  await expect(search.getByText("task-1", { exact: true })).toHaveCount(0);
  await search.getByRole("combobox").press("Enter");

  await expect(page).toHaveURL(/\/w\/acme\/apps\/app-1$/);
});

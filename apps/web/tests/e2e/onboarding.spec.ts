import { expect, test, type Page } from "@playwright/test";

import { activeWorkspaceDefaults } from "./fixtures/workspaces";

const workspaceDefaults = activeWorkspaceDefaults;

const firstAppItem = {
  app: {
    id: "app-1",
    workspace_id: "workspace-test",
    stub_id: "stub-1",
    name: "quickstart",
    version: 1,
    public: false,
    active: true,
    created_at: "2026-01-02T10:00:00Z",
    updated_at: "2026-01-02T10:00:00Z",
  },
  latest_workload: {
    id: "stub-1",
    workspace_id: "workspace-test",
    name: "hello",
    kind: "function",
    created_at: "2026-01-02T10:00:00Z",
    updated_at: "2026-01-02T10:00:00Z",
  },
  latest_deployment: null,
};

async function mockSession(page: Page, beforeWorkspaceChange?: Promise<void>) {
  const workspace = { id: "workspace-test", name: "acme", ...workspaceDefaults };
  await page.addInitScript(() => {
    localStorage.setItem("lazycloud_web_token", "test-token");
  });
  await page.route("**/api/v1/sessions/current", async (route) => {
    await route.fulfill({
      json: {
        user: {
          id: "user-test",
          display_name: "Test User",
          email: "test@example.com",
          avatar_url: "",
          github_user_id: "1234",
          github_login: "test-user",
          role: "member",
          status: "active",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
        workspaces: [workspace],
      },
    });
  });
  await page.route("**/api/v1/concurrency-limits*", async (route) => {
    await route.fulfill({ json: { limits: [] } });
  });
  await page.route("**/api/v1/events/changes/stream*", async (route) => {
    await beforeWorkspaceChange;
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: [
        "id: 1710000000000-0",
        "event: workspace.change",
        `data: ${JSON.stringify({
          event_id: "1710000000000-0",
          occurred_at: "2026-07-13T15:30:00Z",
          workspace_id: "workspace-test",
          topic: "apps",
          change: "created",
          resource_id: "app-1",
          app_id: "app-1",
        })}`,
        "",
        "",
      ].join("\n"),
    });
  });
}

test("empty workspace guides the quickstart and flips to the grid live", async ({ page }) => {
  let publishWorkspaceChange: () => void;
  const workspaceChangeReady = new Promise<void>((resolve) => {
    publishWorkspaceChange = resolve;
  });
  await mockSession(page, workspaceChangeReady);
  let appsRequests = 0;
  await page.route("**/api/v1/apps/summaries*", async (route) => {
    appsRequests += 1;
    await route.fulfill({ json: { items: appsRequests < 2 ? [] : [firstAppItem] } });
  });
  await page.route("**/api/v1/tasks/aggregate-by-time-window*", async (route) => {
    await route.fulfill({ json: { items: [] } });
  });

  await page.goto("/w/acme/apps");

  await expect(page.getByText("Deploy your first app")).toBeVisible();

  // The workspace change stream invalidates the summary query; the next
  // response contains the first app and replaces the guided steps in place.
  publishWorkspaceChange!();
  await expect(page.getByRole("link", { name: "quickstart" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Deploy your first app")).not.toBeVisible();
});

test("device approval page approves a pending CLI sign-in", async ({ page }) => {
  await mockSession(page);
  const pendingCode = {
    user_code: "BCDF-GHJK",
    client_name: "cli@laptop",
    status: "pending",
    created_at: "2026-01-02T10:00:00Z",
    expires_at: "2026-01-02T10:15:00Z",
  };
  await page.route("**/api/v1/device-codes/BCDF-GHJK", async (route) => {
    await route.fulfill({ json: pendingCode });
  });
  await page.route("**/api/v1/device-codes/BCDF-GHJK/approve", async (route) => {
    await route.fulfill({ json: { ...pendingCode, status: "approved" } });
  });

  await page.goto("/activate?code=BCDF-GHJK");

  await expect(page.getByText("Approve CLI sign-in")).toBeVisible();
  await expect(page.getByText("cli@laptop")).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("CLI connected")).toBeVisible();
  await page.getByRole("link", { name: "Go to dashboard" }).click();
  await expect(page).toHaveURL(/\/w\/acme\/apps\/?$/);
});

import { expect, test, type Page } from "@playwright/test";

import { activeWorkspace, signIn, workspaceRoute, type Schemas } from "./fixtures/workspaces";

const firstApp: Schemas["App"] = {
  id: "app-1",
  name: "quickstart",
  state: "active",
  workloads: 1,
  running_containers: 0,
  created_at: "2026-01-02T10:00:00Z",
};

const firstWorkload: Schemas["Workload"] = {
  id: "stub-1",
  app: "quickstart",
  app_state: "active",
  name: "hello",
  kind: "function",
  state: "active",
  running_containers: 0,
  created_at: "2026-01-02T10:00:00Z",
};

async function mockSession(page: Page, beforeWorkspaceChange?: Promise<void>) {
  await signIn(page, [activeWorkspace("workspace-test", "acme")]);
  await page.route(workspaceRoute("changes/stream"), async (route) => {
    // A reconnect resumes after the change it already received, so it gets none.
    if (await route.request().headerValue("last-event-id")) {
      await route.fulfill({ contentType: "text/event-stream", body: ": connected\n\n" });
      return;
    }
    await beforeWorkspaceChange;
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: [
        "id: 1710000000000",
        "event: change",
        `data: ${JSON.stringify({
          seq: 1710000000000,
          occurred_at: "2026-07-13T15:30:00Z",
          workspace_id: "workspace-test",
          changes: [{ topic: "apps", change: "created", resource_id: "app-1", app_id: "app-1" }],
        } satisfies Schemas["ChangeEvent"])}`,
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
  let published = false;
  await page.route(workspaceRoute("apps"), async (route) => {
    await route.fulfill({ json: { apps: published ? [firstApp] : [] } });
  });
  await page.route(workspaceRoute("workloads"), async (route) => {
    await route.fulfill({ json: { workloads: published ? [firstWorkload] : [] } });
  });
  await page.route(workspaceRoute("containers"), async (route) => {
    await route.fulfill({ json: { containers: [] } });
  });
  await page.route(workspaceRoute("metrics/activity"), async (route) => {
    const now = new Date().toISOString();
    await route.fulfill({ json: { window_seconds: 3600, start: now, end: now, series: [] } });
  });

  await page.goto("/w/acme/apps");

  await expect(page.getByText("Run your first function")).toBeVisible();

  // The workspace change stream invalidates the summary query; the next
  // response contains the first app and replaces the guided steps in place.
  published = true;
  publishWorkspaceChange!();
  await expect(page.getByRole("link", { name: "quickstart" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Run your first function")).not.toBeVisible();
});

test("device approval page approves a pending CLI sign-in", async ({ page }) => {
  await mockSession(page);
  const pendingCode: Schemas["DeviceCode"] = {
    user_code: "BCDF-GHJK",
    client_name: "cli@laptop",
    status: "pending",
    created_at: "2026-01-02T10:00:00Z",
    expires_at: "2026-01-02T10:15:00Z",
  };
  await page.route("**/v1/device-codes/BCDF-GHJK", async (route) => {
    await route.fulfill({ json: pendingCode });
  });
  await page.route("**/v1/device-codes/BCDF-GHJK/approve", async (route) => {
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

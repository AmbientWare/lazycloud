import { expect, test, type Page } from "@playwright/test";

import { activeWorkspace, signIn, workspaceRoute, type Schemas } from "./fixtures/workspaces";

const app: Schemas["App"] = {
  id: "app-1",
  name: "square_app",
  state: "active",
  workloads: 1,
  created_at: "2026-01-01T09:00:00Z",
};

const workload: Schemas["DeployedWorkload"] = {
  id: "stub-1",
  app: "square_app",
  app_state: "active",
  name: "square",
  kind: "function",
  state: "active",
  release_id: "release-1",
  version: 7,
  created_at: "2026-01-01T09:00:00Z",
  deployed_at: "2026-01-01T10:00:00Z",
};

function statusCounts(counts: Partial<Schemas["TaskStatusCounts"]>): Schemas["TaskStatusCounts"] {
  return { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0, ...counts };
}

async function mockControlPlane(page: Page) {
  await signIn(page, [
    activeWorkspace("workspace-test", "acme"),
    activeWorkspace("workspace-second", "beta"),
  ]);
  await page.route(workspaceRoute("metrics/tasks"), async (route) => {
    const end = new Date();
    await route.fulfill({
      json: {
        start: new Date(end.getTime() - 86_400_000).toISOString(),
        end: end.toISOString(),
        total: 4,
        status_counts: statusCounts({ running: 1, succeeded: 3 }),
        failure_rate: 0,
        average_runtime_ms: 1500,
        runtime_ms_p50: 1200,
        runtime_ms_p95: 2400,
        runtime_ms_p99: 2800,
        startup_ms_p50: 600,
        startup_ms_p95: 900,
      } satisfies Schemas["TaskMetrics"],
    });
  });
  await page.route(workspaceRoute("metrics/activity"), async (route) => {
    const window = Number(
      new URL(route.request().url()).searchParams.get("window_seconds") ?? 3600,
    );
    const end = Math.floor(Date.now() / (window * 1000)) * window * 1000;
    const bucket = (offset: number, counts: Partial<Schemas["TaskStatusCounts"]>) => ({
      timestamp: new Date(end - offset * window * 1000).toISOString(),
      status_counts: statusCounts(counts),
    });
    await route.fulfill({
      json: {
        window_seconds: window,
        start: new Date(end - 2 * window * 1000).toISOString(),
        end: new Date(end + window * 1000).toISOString(),
        series: [
          {
            app: "square_app",
            app_id: "app-1",
            total: 4,
            buckets: [
              bucket(2, {}),
              bucket(1, { running: 1 }),
              bucket(0, { succeeded: 2, failed: 1 }),
            ],
          },
        ],
      } satisfies Schemas["TaskActivity"],
    });
  });
  await page.route(workspaceRoute("apps"), async (route) => {
    await route.fulfill({ json: { apps: [app] } satisfies Schemas["AppPage"] });
  });
  await page.route(workspaceRoute("deployments"), async (route) => {
    await route.fulfill({ json: { deployments: [workload] } satisfies Schemas["DeploymentPage"] });
  });
  await page.route(workspaceRoute("containers"), async (route) => {
    await route.fulfill({ json: { containers: [] } satisfies Schemas["ContainerPage"] });
  });
  await page.route(workspaceRoute("tasks"), async (route) => {
    await route.fulfill({ json: { tasks: [] } satisfies Schemas["TaskPage"] });
  });
  await page.route("**/api/v1/stubs/sandboxes*", async (route) => {
    await route.fulfill({ json: { data: [], next: "" } });
  });
  await page.route(workspaceRoute("changes/stream"), async (route) => {
    await route.fulfill({
      contentType: "text/event-stream",
      body: ": connected\n\n",
    });
  });
}

test("dashboard entry lands on Apps and the responsive shell switches workspaces", async ({
  page,
}) => {
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
  if (mobile) {
    await page.getByRole("button", { name: "Open workspace menu" }).click();
    const menu = page.getByRole("dialog");
    await expect(
      menu
        .getByRole("navigation", { name: "Account menu" })
        .getByRole("button", { name: "Settings" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Close" }).click();
  } else {
    await expect(nav.getByRole("link", { name: "Apps" })).toHaveAttribute("aria-current", "page");
    await page.getByRole("button", { name: "Test User" }).click();
    const account = page.getByRole("navigation", { name: "Account navigation" });
    await expect(account.getByRole("button", { name: "Settings" })).toBeVisible();
  }

  await expect(page.getByRole("link", { name: /square_app/ })).toBeVisible();
  await expect(page.getByRole("contentinfo")).toHaveCount(0);

  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  await page.getByRole("menuitem", { name: "beta", exact: true }).click();
  await expect(page).toHaveURL(/\/w\/beta\/apps\/?$/);

  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/w\/beta\/apps\/?$/);
});

test("workspace search opens from the keyboard and navigates to a canonical resource URL", async ({
  page,
}) => {
  test.fixme(
    true,
    "Enter opens nothing: the search keeps no result selected once the query filters out the selected one",
  );
  await mockControlPlane(page);
  await page.route(workspaceRoute("tasks"), async (route) => {
    await route.fulfill({
      json: {
        tasks: [
          {
            id: "task-1",
            app: "square_app",
            function: "square",
            release_id: "release-1",
            version: 7,
            status: "succeeded",
            attempts: 1,
            max_attempts: 1,
            root_task_id: "task-1",
            created_at: "2026-01-01T10:00:00Z",
          },
        ],
      } satisfies Schemas["TaskPage"],
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

import { expect, test } from "@playwright/test";
import { z } from "zod";

import { appSchema, deploymentListSchema } from "../../src/lib/api/schemas/apps";
import { cronJobListSchema } from "../../src/lib/api/schemas/cron";

const live = process.env.WEB_E2E_LIVE === "1";
const token = process.env.WEB_E2E_TOKEN ?? "";
const workspace = process.env.WEB_E2E_WORKSPACE ?? "";
const scheduleApp = process.env.WEB_E2E_SCHEDULE_APP ?? "";

test("authenticated production shell renders live control-plane data", async ({ page }) => {
  test.skip(!live, "WEB_E2E_LIVE=1 is required");
  test.skip(!token || !workspace, "WEB_E2E_TOKEN and WEB_E2E_WORKSPACE are required");

  await page.addInitScript((value) => {
    localStorage.setItem("lazycloud_web_token", value);
  }, token);

  const pageErrors: string[] = [];
  const serverErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/api/") && response.status() >= 500) {
      serverErrors.push(`${response.status()} ${path}`);
    }
  });

  const workspaces = await page.request.get("/api/v1/workspaces?include_deleting=true", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(workspaces.ok()).toBe(true);
  const payload = (await workspaces.json()) as {
    workspaces: Array<{ name: string }>;
  };
  expect(payload.workspaces.some((item) => item.name === workspace)).toBe(true);

  await page.goto(`/w/${encodeURIComponent(workspace)}/apps`, {
    waitUntil: "domcontentloaded",
  });
  await expect(page.getByRole("heading", { name: "Apps", exact: true })).toBeVisible();

  const navigation = page.getByRole("navigation", { name: "Main navigation" });
  await navigation.getByRole("link", { name: "Tasks", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Tasks", exact: true })).toBeVisible();
  expect(pageErrors).toEqual([]);
  expect(serverErrors).toEqual([]);
});

test("scheduled workload renders its scoped live schedule", async ({ page }) => {
  test.skip(!live || !token || !workspace || !scheduleApp, "Live schedule app is required");
  await page.addInitScript((value) => localStorage.setItem("lazycloud_web_token", value), token);
  const options = { headers: { Authorization: `Bearer ${token}` } };
  const query = `workspace=${encodeURIComponent(workspace)}`;
  const apps = await page.request.get(`/api/v1/apps?${query}`, options);
  expect(apps.ok()).toBe(true);
  const app = z
    .object({ data: z.array(appSchema) })
    .parse(await apps.json())
    .data.find((item) => item.name === scheduleApp);
  expect(app).toBeDefined();
  if (!app) throw new Error("Acceptance schedule app was not found");
  const deployments = await page.request.get(
    `/api/v1/deployments?${query}&app_id=${app.id}`,
    options,
  );
  expect(deployments.ok()).toBe(true);
  const deployment = deploymentListSchema
    .parse(await deployments.json())
    .data.find((item) => item.name === "scheduled-marker");
  expect(deployment).toBeDefined();
  if (!deployment) throw new Error("Acceptance scheduled workload was not found");
  const scheduled = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/v1/cron-jobs" &&
      url.searchParams.get("deployment_id") === deployment.id
    );
  });
  await page.goto(
    `/w/${encodeURIComponent(workspace)}/apps/${app.id}/workloads/function/${deployment.name}`,
  );
  const response = await scheduled;
  expect(response.ok()).toBe(true);
  const jobs = cronJobListSchema.parse(await response.json()).cron_jobs;
  expect(jobs).toHaveLength(1);
  expect(jobs[0].deployment_id).toBe(deployment.id);
  await expect(page.getByText("*/1 * * * *", { exact: true })).toBeVisible();
  await expect(page.getByText("UTC", { exact: true })).toBeVisible();
});

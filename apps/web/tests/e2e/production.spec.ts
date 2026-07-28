import { expect, test } from "@playwright/test";

const live = process.env.WEB_E2E_LIVE === "1";
const token = process.env.WEB_E2E_TOKEN ?? "";
const workspace = process.env.WEB_E2E_WORKSPACE ?? "";

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

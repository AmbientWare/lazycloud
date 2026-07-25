import { AxeBuilder } from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("canonical marketing routes are public, responsive, and accessible", async ({
  page,
}) => {
  const authenticatedRequests: string[] = [];
  const consoleErrors: string[] = [];
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/api/") || pathname.startsWith("/auth/")) {
      authenticatedRequests.push(pathname);
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await page.emulateMedia({ reducedMotion: "reduce" });

  await page.goto("/");

  await expect(page).toHaveTitle(
    "LazyCloud — The cloud platform for AI-speed development",
  );
  await expect(page.locator('meta[name="description"]')).toHaveAttribute(
    "content",
    /complete cloud platform for developers and agents to deploy applications/i,
  );
  await expect(page.getByRole("heading", { level: 1 })).toHaveAccessibleName(
    "The cloud platform for AI-speed development.",
  );
  await expect(page.locator("body")).not.toContainText("Enter an access token");
  await expect(page.locator("body")).toContainText(
    "lazycloud deploy application.py:app",
  );
  await expect(page.locator("body")).not.toContainText("lazycloud-admin");

  const heroTabs = page
    .getByRole("tablist", { name: "Hero code examples" })
    .getByRole("tab");
  await expect(heroTabs).toHaveText([
    "Apps + APIs",
    "Functions",
    "Task queues",
    "Sandboxes",
    "Services",
    "Schedules",
  ]);
  await expect(heroTabs.first()).toHaveAttribute("aria-selected", "true");

  await page.getByRole("tab", { name: "Sandboxes" }).click();
  const sandboxPanel = page.getByRole("tabpanel", { name: "Sandboxes" });
  await expect(sandboxPanel).toContainText("python application.py");
  await expect(sandboxPanel).toContainText("sandbox ready");
  await expect(sandboxPanel).not.toContainText(
    "lazycloud deploy application.py:app",
  );

  const marketingSurface = page.locator(".marketing-site");
  await expect(marketingSurface.locator('a[href="/dashboard"]')).toHaveCount(0);
  await expect(
    marketingSurface.getByRole("button", {
      name: "Private beta",
      includeHidden: true,
    }),
  ).toHaveCount(3);
  await expect(marketingSurface).toContainText(
    "lazycloud client get review_app",
  );
  await expect(marketingSurface).toContainText(
    'Volume("artifacts") · /artifacts',
  );
  const layout = await marketingSurface.evaluate((element) => ({
    horizontalOverflow: element.scrollWidth - element.clientWidth,
    height: element.clientHeight,
    viewportHeight: window.innerHeight,
    width: element.clientWidth,
    viewportWidth: window.innerWidth,
  }));
  expect(layout.horizontalOverflow).toBeLessThanOrEqual(1);
  expect(layout.height).toBe(layout.viewportHeight);
  expect(layout.width).toBe(layout.viewportWidth);

  await marketingSurface.evaluate((element) =>
    element.scrollTo({ top: element.scrollHeight }),
  );
  await expect(page.getByRole("contentinfo")).toBeVisible();
  const accessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  expect(
    accessibility.violations.map((violation) => violation.id),
    "Marketing accessibility violations",
  ).toEqual([]);
  expect(authenticatedRequests).toEqual([]);
  expect(consoleErrors).toEqual([]);
});

test("dashboard entry remains protected while marketing routes stay public", async ({
  page,
}) => {
  const workspaceRequests: string[] = [];
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/api/v1/workspaces"))
      workspaceRequests.push(pathname);
  });
  await page.route("**/auth/bootstrap", (route) =>
    route.fulfill({ json: { required: false, token_count: 1 } }),
  );

  await page.goto("/dashboard");

  await expect(
    page.getByRole("heading", { name: "Enter an access token" }),
  ).toBeVisible();
  expect(workspaceRequests).toEqual([]);
});

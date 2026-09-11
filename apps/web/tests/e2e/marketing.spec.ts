import { AxeBuilder } from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { activeWorkspaceDefaults } from "./fixtures/workspaces";

test("canonical marketing routes are public, responsive, and accessible", async ({ page }) => {
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

  // The public route is not behind the sign-in gate, and the internal admin CLI
  // (`apps/cli`) is never advertised on a customer-facing page. Matched on the
  // gate's heading rather than its text, so a marketing sign-in link stays legal.
  await expect(page.getByRole("heading", { name: "Sign in" })).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("lazycloud-admin");

  const sandboxTab = page.getByRole("tab", { name: "Sandboxes" });
  await sandboxTab.click();
  await expect(sandboxTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel", { name: "Sandboxes" })).toBeVisible();

  const marketingSurface = page.locator(".marketing-site");
  await expect(marketingSurface.locator('a[href="/dashboard"]')).toHaveCount(0);
  const horizontalOverflow = await marketingSurface.evaluate(
    (element) => element.scrollWidth - element.clientWidth,
  );
  expect(horizontalOverflow).toBeLessThanOrEqual(1);

  await marketingSurface.evaluate((element) => element.scrollTo({ top: element.scrollHeight }));
  await expect(page.getByRole("contentinfo")).toBeVisible();
  const accessibility = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(
    accessibility.violations.map((violation) => violation.id),
    "Marketing accessibility violations",
  ).toEqual([]);

  // Pricing is the one navigation entry with a page behind it, so it links
  // rather than sitting inert like the destinations that have none.
  await expect(marketingSurface.getByRole("button", { name: "Pricing" })).toHaveCount(0);
  expect(await marketingSurface.locator('a[href="/pricing"]').count()).toBeGreaterThan(0);

  await page.goto("/pricing");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  const pricingOverflow = await marketingSurface.evaluate(
    (element) => element.scrollWidth - element.clientWidth,
  );
  expect(pricingOverflow).toBeLessThanOrEqual(1);
  const pricingAccessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  expect(
    pricingAccessibility.violations.map((violation) => violation.id),
    "Pricing accessibility violations",
  ).toEqual([]);

  expect(authenticatedRequests).toEqual([]);
  expect(consoleErrors).toEqual([]);
});

test("dashboard entry remains protected while marketing routes stay public", async ({ page }) => {
  const workspaceRequests: string[] = [];
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/api/v1/workspaces")) workspaceRequests.push(pathname);
  });
  await page.route("**/auth/bootstrap", (route) =>
    route.fulfill({ json: { required: false, token_count: 1 } }),
  );

  await page.goto("/dashboard");

  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  expect(workspaceRequests).toEqual([]);
});

test("an existing session enters the dashboard without reopening GitHub", async ({ page }) => {
  const authStarts: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/auth/github/start") authStarts.push(request.url());
  });
  await page.addInitScript(() => {
    localStorage.setItem("lazycloud_web_token", "test-token");
  });
  await page.route("**/api/v1/sessions/current", (route) =>
    route.fulfill({
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
        workspaces: [{ id: "workspace-test", name: "acme", ...activeWorkspaceDefaults }],
      },
    }),
  );

  await page.goto("/");
  await page
    .getByRole("link", { name: /^Dashboard/ })
    .first()
    .click();

  await expect(page).toHaveURL(/\/w\/acme\/apps\/?$/);
  expect(authStarts).toEqual([]);
});

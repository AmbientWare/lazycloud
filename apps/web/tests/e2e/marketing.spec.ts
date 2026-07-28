import { AxeBuilder } from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

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

  // The public route is not behind the token gate, and the internal admin CLI
  // (`apps/cli`) is never advertised on a customer-facing page.
  await expect(page.locator("body")).not.toContainText("Enter an access token");
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

  await expect(page.getByRole("heading", { name: "Enter an access token" })).toBeVisible();
  expect(workspaceRequests).toEqual([]);
});

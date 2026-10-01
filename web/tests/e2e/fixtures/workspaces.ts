import type { Page } from "@playwright/test";

import type { components } from "../../../src/lib/api/generated/openapi";

export type Schemas = components["schemas"];

export function activeWorkspace(id: string, name: string): Schemas["Workspace"] {
  return { id, name, state: "active", role: "member", created_at: "2026-01-01T00:00:00Z" };
}

export const testUser: Schemas["User"] = {
  id: "user-test",
  display_name: "Test User",
  email: "test@example.com",
  avatar_url: "",
  github_login: "test-user",
  is_admin: false,
  status: "active",
  created_at: "2026-01-01T00:00:00Z",
};

/**
 * Leave the page as the sign-in callback does: the HttpOnly cookie is the
 * server's, so the page keeps only its session marker, and `GET /v1/me`
 * answers with the account and the workspaces it reaches.
 */
export async function signIn(page: Page, workspaces: Schemas["Workspace"][]) {
  await page.addInitScript(() => {
    localStorage.setItem("lazycloud_web_token", "session");
  });
  // A read the spec does not mock never reaches a real server, whose 401
  // would sign the page out. Routes added later take precedence.
  await page.route(/\/v1\//, (route) =>
    route.fulfill({ status: 404, json: { code: "not_found", message: "not mocked" } }),
  );
  await page.route("**/v1/me", (route) =>
    route.fulfill({ json: { user: testUser, workspaces } satisfies Schemas["Me"] }),
  );
}

/** A workspace-scoped collection route, such as `apps` for `/v1/workspaces/{name}/apps`. */
export function workspaceRoute(collection: string): RegExp {
  return new RegExp(`/v1/workspaces/[^/]+/${collection}(?:\\?.*)?$`);
}

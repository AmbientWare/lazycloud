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

const freeEntitlements: Schemas["PlanEntitlements"] = {
  max_concurrent_cpu_containers: 30,
  max_concurrent_gpus: 5,
  gpu_types: ["T4"],
  max_workspaces: 1,
  max_members: 1,
  connected_cloud: false,
  custom_domains: false,
  self_hosted: true,
  region_selection: false,
  retention_days: 1,
  max_workspace_disk_gib: 0,
};

/** What the public `GET /v1/pricing` answers: one plan and one automatic placement. */
export const pricingCatalog: Schemas["PricingCatalog"] = {
  pricing_version: "e2e",
  metered_rates_effective_at: "2026-09-11T00:00:00Z",
  currency: "USD",
  connected_cloud_management_fee_percent: 8,
  credit_purchase: { minimum_cents: 2000, maximum_cents: 100000 },
  trial: { amount_nanos: 2_000_000_000, duration_days: 30, one_time: true },
  no_payment_method: { max_concurrent_cpu_containers: 10, max_concurrent_gpus: 1, gpu_types: [] },
  plans: [
    {
      id: "free",
      terms_version: "free-v2",
      name: "Free",
      summary: "Free plan",
      monthly_nanos: 0,
      included_nanos: 0,
      entitlements: freeEntitlements,
      terms: [],
    },
  ],
  shape_rates: [],
  gpu_rates: [
    {
      gpu_type: "T4",
      enabled: true,
      nanos_per_card_hour: { platform_fleet: 0, connected_cloud: 0, self_hosted: 0 },
    },
  ],
  placement_rates: [
    {
      rate_class: "auto",
      name: "Automatic",
      effective_at: "2026-09-10T00:00:00Z",
      pinned: false,
      preemptible: true,
      cpu_memory_multiplier: 1,
      gpu_multiplier: 1,
      compute_rates: [
        {
          billing_owner: "platform_fleet",
          nanos_per_container_hour: 0,
          nanos_per_cpu_hour: 44_000_000,
          nanos_per_memory_gib_hour: 7_500_000,
          nanos_per_gpu_card_hour: 0,
        },
        {
          billing_owner: "platform_fleet",
          gpu_type: "T4",
          nanos_per_container_hour: 0,
          nanos_per_cpu_hour: 44_000_000,
          nanos_per_memory_gib_hour: 7_500_000,
          nanos_per_gpu_card_hour: 350_000_000,
        },
      ],
    },
  ],
  platform_rate: {
    nanos_per_egress_gib: 0,
    nanos_per_volume_gib_month: 50_000_000,
    storage_month_seconds: 2_592_000,
  },
};

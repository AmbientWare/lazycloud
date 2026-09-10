import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { authTokenSchema } from "@/lib/api/schemas";
import { testQueryClient } from "@/test/query-client";

import { AccessTokens } from ".";

it("shows CLI login tokens only when the device toggle is on", async () => {
  const manual = authTokenSchema.parse({
    id: "manual",
    name: "cli@manual-api-token",
    prefix: "rt_manual",
    kind: "user",
    device_login: false,
    user_id: "account",
    workspace_id: "",
    status: "active",
    scopes: ["*"],
    reusable: true,
    disabled_by_admin: false,
    created_at: "2026-09-10T12:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
  });
  const device = { ...manual, id: "device", name: "work-laptop", device_login: true };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL(String(input), "http://localhost");
    return Response.json({
      data: url.searchParams.get("include_device") === "false" ? [manual] : [device, manual],
      next: "",
    });
  });

  render(
    <QueryClientProvider client={testQueryClient()}>
      <AccessTokens />
    </QueryClientProvider>,
  );
  const toggle = screen.getByRole("switch", { name: "Show device tokens" });
  expect(toggle).not.toBeChecked();
  expect(await screen.findByText(manual.name)).toBeVisible();
  expect(screen.queryByText(device.name)).not.toBeInTheDocument();

  fireEvent.click(toggle);
  expect(await screen.findByText(device.name)).toBeVisible();
  expect(screen.getByText(manual.name)).toBeVisible();

  fireEvent.click(toggle);
  await waitFor(() => expect(screen.queryByText(device.name)).not.toBeInTheDocument());
  expect(screen.getByText(manual.name)).toBeVisible();
});

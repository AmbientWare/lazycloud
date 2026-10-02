import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { testQueryClient } from "@/test/query-client";

import { AccessTokens } from ".";

it("shows CLI login tokens only when the device toggle is on", async () => {
  const manual = {
    id: "manual",
    name: "cli@manual-api-token",
    device: false,
    status: "active",
    created_at: "2026-09-10T12:00:00Z",
    prefix: "lc_man",
  };
  const device = { ...manual, id: "device", name: "work-laptop", device: true };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL((input as Request).url);
    return Response.json({
      tokens: url.searchParams.get("include_device") === "false" ? [manual] : [device, manual],
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
  expect(screen.getByText("lc_man...")).toBeVisible();
  expect(screen.queryByText(device.name)).not.toBeInTheDocument();

  fireEvent.click(toggle);
  expect(await screen.findByText(device.name)).toBeVisible();
  expect(screen.getByText(manual.name)).toBeVisible();

  fireEvent.click(toggle);
  await waitFor(() => expect(screen.queryByText(device.name)).not.toBeInTheDocument());
  expect(screen.getByText(manual.name)).toBeVisible();
});

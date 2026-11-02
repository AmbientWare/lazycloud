import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { WorkspaceContext } from "@/lib/workspace-context";
import { testQueryClient } from "@/test/query-client";

import { WebhookSettings } from "./WebhookSettings";

const workspace: Schemas["Workspace"] = {
  id: "workspace-1",
  name: "acme",
  state: "active",
  created_at: "2026-09-07T00:00:00Z",
};

it("sends the chosen events for every app and shows the server's refusal", async () => {
  // The checkbox measures itself.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  const created: unknown[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const request = input as Request;
    if (request.method === "POST") {
      created.push(await request.json());
      return Response.json(
        { code: "limit_reached", message: "a workspace holds at most 20 webhooks" },
        { status: 409 },
      );
    }
    return Response.json({ webhooks: [] });
  });

  render(
    <QueryClientProvider client={testQueryClient()}>
      <WorkspaceContext.Provider value={{ workspace, workspaces: [workspace] }}>
        <WebhookSettings />
      </WorkspaceContext.Provider>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("No webhooks yet")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "Add webhook" }));
  fireEvent.change(screen.getByPlaceholderText("https://example.com/hooks/lazycloud"), {
    target: { value: "https://hooks.acme.dev/lazycloud" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: /Task failed after its retries/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Version stopped starting/ }));
  fireEvent.click(
    within(screen.getByRole("form", { name: "New webhook" })).getByRole("button", {
      name: "Add webhook",
    }),
  );

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "a workspace holds at most 20 webhooks",
  );
  expect(created).toEqual([
    { url: "https://hooks.acme.dev/lazycloud", events: ["task.failed", "workload.failed"] },
  ]);
});

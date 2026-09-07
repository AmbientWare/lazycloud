import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { SecretsTab } from "./SecretsTab";

const secret = {
  name: "API_KEY",
  value: "********",
  created_at: "2026-09-07T00:00:00Z",
  updated_at: "2026-09-07T00:00:00Z",
  workloads: [],
};

beforeEach(() => vi.useFakeTimers());

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("keeps a pending reveal masked during deletion and allows a fresh reveal after keeping it", async () => {
  let resolveReveal: ((response: Response) => void) | undefined;
  let response = new Promise<Response>((resolve) => {
    resolveReveal = resolve;
  });
  vi.stubGlobal("fetch", () => response);
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  client.setQueryData(workspaceQueryKeys.storage.secrets("workspace-1"), { secrets: [secret] });
  render(
    <QueryClientProvider client={client}>
      <SecretsTab
        workspaceId="workspace-1"
        workspaceName="workspace"
        creating={false}
        onCreatingChange={() => {}}
      />
    </QueryClientProvider>,
  );

  fireEvent.click(screen.getByRole("button", { name: "Reveal secret API_KEY" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete secret API_KEY" }));
  await act(async () => {
    resolveReveal?.(Response.json({ secret: { name: secret.name, value: "test-only-old-value" } }));
  });
  expect(screen.queryByText("test-only-old-value")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reveal secret API_KEY" })).toBeDisabled();

  let finishDelete: ((response: Response) => void) | undefined;
  response = new Promise<Response>((resolve) => {
    finishDelete = resolve;
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Keep" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Keep" }));
  expect(screen.getByRole("button", { name: "Reveal secret API_KEY" })).toBeDisabled();
  await act(async () => {
    finishDelete?.(Response.json({ detail: "Deletion failed" }, { status: 503 }));
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(screen.getByText("Deletion failed")).toBeVisible();
  expect(screen.getByRole("button", { name: "Keep" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Keep" }));
  response = Promise.resolve(
    Response.json({ secret: { name: secret.name, value: "test-only-current-value" } }),
  );
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Reveal secret API_KEY" })),
  );
  expect(screen.getByText("test-only-current-value")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Hide secret API_KEY" }));
  expect(screen.queryByText("test-only-current-value")).not.toBeInTheDocument();
  client.clear();
});

it("clears revealed values on rotation and ignores responses for the previous version", async () => {
  let response = Promise.resolve(
    Response.json({ secret: { name: secret.name, value: "test-only-old-value" } }),
  );
  vi.stubGlobal("fetch", () => response);
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  const queryKey = workspaceQueryKeys.storage.secrets("workspace-1");
  client.setQueryData(queryKey, { secrets: [secret] });
  render(
    <QueryClientProvider client={client}>
      <SecretsTab
        workspaceId="workspace-1"
        workspaceName="workspace"
        creating={false}
        onCreatingChange={() => {}}
      />
    </QueryClientProvider>,
  );

  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Reveal secret API_KEY" })),
  );
  expect(screen.getByText("test-only-old-value")).toBeVisible();
  await act(async () => {
    client.setQueryData(queryKey, {
      secrets: [{ ...secret, updated_at: "2026-09-07T00:01:00Z" }],
    });
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(screen.queryByText("test-only-old-value")).not.toBeInTheDocument();

  let resolveReveal: ((response: Response) => void) | undefined;
  response = new Promise<Response>((resolve) => {
    resolveReveal = resolve;
  });
  fireEvent.click(screen.getByRole("button", { name: "Reveal secret API_KEY" }));
  await act(async () => {
    client.setQueryData(queryKey, {
      secrets: [{ ...secret, updated_at: "2026-09-07T00:02:00Z" }],
    });
    await vi.advanceTimersByTimeAsync(1);
  });
  await act(async () => {
    resolveReveal?.(
      Response.json({ secret: { name: secret.name, value: "test-only-stale-value" } }),
    );
  });
  expect(screen.queryByText("test-only-stale-value")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reveal secret API_KEY" })).toBeEnabled();
  client.clear();
});

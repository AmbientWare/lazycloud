import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { Workspace } from "@/lib/api/schemas";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { WorkspaceContext } from "@/lib/workspace-context";

import { SandboxFileBrowser } from ".";

const workspace: Workspace = {
  id: "workspace-1",
  name: "workspace",
  status: "active",
  signing_key_prefix: null,
  primary_token_id: null,
  concurrency_limit_id: null,
  storage: { backend: "s3", bucket: null, prefix: "" },
  labels: {},
  metadata: {},
  created_at: "2026-09-07T00:00:00Z",
  updated_at: "2026-09-07T00:00:00Z",
};
let client: QueryClient;

beforeEach(() => {
  vi.useFakeTimers();
  client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
});

afterEach(() => {
  cleanup();
  client.clear();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("keeps late previews from replacing a newer selection or reopening after navigation", async () => {
  let completeOld: ((response: Response) => void) | undefined;
  let response = new Promise<Response>((resolve) => {
    completeOld = resolve;
  });
  vi.stubGlobal("fetch", () => response);
  renderBrowser();
  fireEvent.click(screen.getByRole("button", { name: /^old\.txt/ }));

  response = Promise.resolve(Response.json({ value_base64: btoa("current content") }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /^new\.txt/ })));
  await act(async () => completeOld?.(Response.json({ value_base64: btoa("old content") })));
  expect(screen.getByText("current content")).toBeVisible();
  expect(screen.queryByText("old content")).not.toBeInTheDocument();

  response = new Promise<Response>((resolve) => {
    completeOld = resolve;
  });
  fireEvent.click(screen.getByRole("button", { name: /^old\.txt/ }));
  fireEvent.click(screen.getByRole("button", { name: "folder" }));
  await act(async () => completeOld?.(Response.json({ value_base64: btoa("old content") })));
  expect(screen.queryByText("old content")).not.toBeInTheDocument();
  expect(screen.queryByText("Reading")).not.toBeInTheDocument();
});

it("invalidates an in-flight preview when its file is deleted", async () => {
  let completePreview: ((response: Response) => void) | undefined;
  const previewResponse = new Promise<Response>((resolve) => {
    completePreview = resolve;
  });
  vi.stubGlobal("fetch", (input: string, init?: RequestInit) =>
    input.includes("/download/")
      ? previewResponse
      : Promise.resolve(Response.json(init?.method === "DELETE" ? {} : { files: [] })),
  );
  renderBrowser();
  fireEvent.click(screen.getByRole("button", { name: /^old\.txt/ }));
  fireEvent.click(screen.getByRole("button", { name: "Delete old.txt" }));
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete old.txt" })),
  );
  await act(async () =>
    completePreview?.(Response.json({ value_base64: btoa("deleted content") })),
  );
  expect(screen.queryByText("deleted content")).not.toBeInTheDocument();
  expect(screen.queryByText("Reading")).not.toBeInTheDocument();
});

it("shows failed previews as errors and catches failed downloads", async () => {
  vi.stubGlobal("fetch", () => Promise.resolve(new Response("File unavailable", { status: 503 })));
  renderBrowser();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /^old\.txt/ })));
  expect(screen.getByRole("alert")).toHaveTextContent("File unavailable");
  expect(screen.getByText("File unavailable").closest("pre")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "workspace" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Download old.txt" })));
  expect(screen.getByRole("alert")).toHaveTextContent("File unavailable");
});

function renderBrowser() {
  const queryKey = workspaceQueryKeys.sandboxes.files(workspace.id, "container-1", "/workspace");
  client.setQueryData(queryKey, {
    files: [
      { name: "old.txt", size: 4, mode: 0, is_dir: false },
      { name: "new.txt", size: 4, mode: 0, is_dir: false },
      { name: "folder", size: 0, mode: 0, is_dir: true },
    ],
  });
  client.setQueryData(
    workspaceQueryKeys.sandboxes.files(workspace.id, "container-1", "/workspace/folder"),
    { files: [] },
  );
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceContext.Provider value={{ workspace, workspaces: [workspace] }}>
        <SandboxFileBrowser containerId="container-1" writable />
      </WorkspaceContext.Provider>
    </QueryClientProvider>,
  );
}

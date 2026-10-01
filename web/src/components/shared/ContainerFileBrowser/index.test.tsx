import { testQueryClient } from "@/test/query-client";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";

import type { Workspace } from "@/lib/api/schemas";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { WorkspaceContext } from "@/lib/workspace-context";

import { ContainerFileBrowser } from ".";

const workspace: Workspace = {
  id: "workspace-1",
  name: "workspace",
  status: "active",
  signing_key_prefix: null,
  primary_token_id: null,
  concurrency_limit_id: null,
  connection_id: null,
  storage: { backend: "s3", bucket: null, prefix: "" },
  labels: {},
  metadata: {},
  created_at: "2026-09-07T00:00:00Z",
  updated_at: "2026-09-07T00:00:00Z",
};
let client: QueryClient;

beforeEach(() => {
  client = testQueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
});

it("draws an SVG only through an image element, so its script never joins the page", async () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>';
  vi.stubGlobal("fetch", () => Promise.resolve(Response.json({ value_base64: btoa(svg) })));
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
  vi.spyOn(URL, "revokeObjectURL").mockReturnValue(undefined);
  // jsdom decodes no images; a real browser would decode this one.
  Object.defineProperty(HTMLImageElement.prototype, "decode", {
    configurable: true,
    value: () => Promise.resolve(),
  });
  renderBrowser();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /^logo\.svg/ })));

  expect(await screen.findByRole("img", { name: "logo.svg" })).toHaveAttribute(
    "src",
    "blob:preview",
  );
  expect(document.querySelector("script, svg:not([aria-hidden])")).toBeNull();
  expect(screen.queryByText(/alert\(1\)/)).not.toBeInTheDocument();
  Reflect.deleteProperty(HTMLImageElement.prototype, "decode");
});

it("shows failed previews as errors and catches failed downloads", async () => {
  vi.stubGlobal("fetch", () => Promise.resolve(new Response("File unavailable", { status: 503 })));
  renderBrowser();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /^old\.txt/ })));
  const dialog = await screen.findByRole("dialog");
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("File unavailable");

  fireEvent.keyDown(dialog, { key: "Escape" });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Download old.txt" })));
  expect(screen.getByRole("alert")).toHaveTextContent("File unavailable");
});

function renderBrowser() {
  const queryKey = workspaceQueryKeys.containers.files(workspace.id, "container-1", "/workspace");
  client.setQueryData(queryKey, {
    files: [
      { name: "old.txt", size: 4, mode: 0, is_dir: false },
      { name: "logo.svg", size: 90, mode: 0, is_dir: false },
    ],
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceContext.Provider value={{ workspace, workspaces: [workspace] }}>
        <ContainerFileBrowser containerId="container-1" rootPath="/workspace" writable />
      </WorkspaceContext.Provider>
    </QueryClientProvider>,
  );
}

import { File as NodeFile } from "node:buffer";

import { testQueryClient } from "@/test/query-client";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { WorkspaceContext } from "@/lib/workspace-context";

import { ContainerFileBrowser } from ".";

const workspace: Schemas["Workspace"] = {
  id: "workspace-1",
  name: "workspace",
  state: "active",
  created_at: "2026-09-07T00:00:00Z",
};
let client: QueryClient;

beforeEach(() => {
  client = testQueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
});

it("draws an SVG only through an image element, so its script never joins the page", async () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>';
  vi.stubGlobal("fetch", () => Promise.resolve(new Response(svg)));
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

it("uploads a file's bytes unchanged to its path in the open directory", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (request: Request) => {
    requests.push(request);
    return Promise.resolve(new Response(null, { status: 204 }));
  });
  const { container } = renderBrowser();
  const bytes = new Uint8Array([0, 1, 2, 255]);
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
  // jsdom's File has no stream(), so Node's fetch would send it as text; a browser sends its bytes.
  const file = new NodeFile([bytes], "data.bin");
  await act(async () => fireEvent.change(input, { target: { files: [file] } }));

  const upload = requests.find((request) => request.method === "PUT")!;
  const url = new URL(upload.url);
  expect(url.pathname).toBe("/v1/workspaces/workspace/containers/container-1/files/content");
  expect(url.searchParams.get("path")).toBe("/workspace/data.bin");
  expect(upload.headers.get("Content-Type")).toBe("application/octet-stream");
  expect(new Uint8Array(await upload.arrayBuffer())).toEqual(bytes);
});

function renderBrowser() {
  const queryKey = workspaceQueryKeys.containers.files(workspace.name, "container-1", "/workspace");
  const file = { mode: 0o100644, permissions: 0o644, owner: "0", group: "0", is_dir: false };
  const listing: Schemas["ContainerFileList"] = {
    files: [
      { ...file, name: "old.txt", size: 4 },
      { ...file, name: "logo.svg", size: 90 },
    ],
    truncated: false,
  };
  client.setQueryData(queryKey, listing);
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceContext.Provider value={{ workspace, workspaces: [workspace] }}>
        <ContainerFileBrowser containerId="container-1" rootPath="/workspace" writable />
      </WorkspaceContext.Provider>
    </QueryClientProvider>,
  );
}

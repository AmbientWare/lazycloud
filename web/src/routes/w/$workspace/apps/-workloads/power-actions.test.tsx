import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { testQueryClient } from "@/test/query-client";

import { DevboxActions } from "./DevboxDetail";
import { PodActions } from "./PodActions";

const workload: Schemas["Workload"] = {
  id: "11111111-1111-4111-8111-111111111111",
  app: "tools",
  name: "box",
  kind: "pod",
  state: "active",
  app_state: "active",
  running_containers: 0,
  created_at: "2026-10-07T10:00:00Z",
};

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

function devbox(state: Schemas["DevboxState"], phase: Schemas["DevboxPhase"]): Schemas["Devbox"] {
  return {
    deployment_id: workload.id,
    name: "box",
    app: "tools",
    ssh_command: "lazycloud devbox box ssh",
    ssh_host: "box.tools.dev",
    state,
    phase,
    open_connections: 0,
  };
}

/** Serves `status` for reads and records each POST's path. */
function serve(status: Schemas["Devbox"] | Schemas["Workload"]): string[] {
  const posts: string[] = [];
  fetchMock.mockImplementation(async (input) => {
    const request = input as Request;
    const path = new URL(request.url).pathname;
    if (request.method === "POST") {
      posts.push(path);
      if (path.endsWith("/devbox/stop")) return Response.json(devbox("stopped", "stopped"));
    }
    return Response.json(status);
  });
  return posts;
}

function renderWith(node: ReactNode) {
  render(<QueryClientProvider client={testQueryClient()}>{node}</QueryClientProvider>);
}

it.each([
  ["waiting for a machine", "queued"],
  ["pulling its image", "pulling_image"],
  ["restoring its disk", "restoring_disk"],
  ["starting", "starting"],
] as const)("cancels a devbox start while it is %s", async (_, phase) => {
  const posts = serve(devbox("starting", phase));
  renderWith(<DevboxActions workspace="dev" workload={workload} />);

  const cancel = await screen.findByRole("button", { name: "Cancel" });
  expect(cancel).toBeEnabled();
  await act(async () => fireEvent.click(cancel));

  expect(posts).toEqual(["/v1/workspaces/dev/apps/tools/workloads/pod/box/devbox/stop"]);
});

it("offers Stop beside Start once a devbox start failed, since it retries", async () => {
  const posts = serve(devbox("stopped", "failed"));
  renderWith(<DevboxActions workspace="dev" workload={workload} />);

  expect(await screen.findByRole("button", { name: "Start" })).toBeEnabled();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop" })));

  expect(posts).toEqual(["/v1/workspaces/dev/apps/tools/workloads/pod/box/devbox/stop"]);
});

it("stops a pod deployment from its header", async () => {
  const posts = serve({ ...workload, state: "stopped" });
  renderWith(<PodActions workspace="dev" workload={workload} />);

  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop" })));

  expect(posts).toEqual(["/v1/workspaces/dev/apps/tools/workloads/pod/box/stop"]);
});

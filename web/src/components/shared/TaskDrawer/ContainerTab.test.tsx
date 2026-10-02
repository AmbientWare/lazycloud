import { testQueryClient } from "@/test/query-client";
import { render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { ContainerTab } from "./ContainerTab";

it("names a function container's image and host, with no pod command or ports", async () => {
  const container: Schemas["Container"] = {
    id: "0199a000-0000-7000-8000-00000000c001",
    app: "shop",
    function: "checkout",
    release_id: "release-1",
    state: "stopped",
    stop_reason: "out_of_memory",
    slots: 1,
    running_tasks: 0,
    cpu_millis: 1000,
    memory_mib: 512,
    image: "registry.example/shop@sha256:abc",
    created_at: "2026-10-01T12:00:00Z",
    ready_at: "2026-10-01T12:00:02Z",
    stopped_at: "2026-10-01T12:01:00Z",
  };
  vi.stubGlobal("fetch", async (input: Request) => {
    const path = new URL(input.url).pathname;
    if (path.endsWith("/metrics")) {
      return Response.json({
        container_id: container.id,
        cpu_total_millicores: 1000,
        memory_total_bytes: 512 * 1024 * 1024,
        step_seconds: 5,
        points: [],
      });
    }
    if (path.endsWith("/lifecycle")) {
      return Response.json({
        container_id: container.id,
        state: "stopped",
        host: "host-a",
        created_at: container.created_at,
        stages: [],
      });
    }
    return Response.json(container);
  });
  render(
    <QueryClientProvider client={testQueryClient()}>
      <ContainerTab workspace="dev" containerId={container.id} waiting={false} live={false} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("registry.example/shop@sha256:abc")).toBeVisible();
  expect(await screen.findByText("host-a")).toBeVisible();
  expect(screen.getByText("It ran out of memory")).toBeVisible();
  expect(screen.queryByText("Command")).not.toBeInTheDocument();
  expect(screen.queryByText("Ports")).not.toBeInTheDocument();
});

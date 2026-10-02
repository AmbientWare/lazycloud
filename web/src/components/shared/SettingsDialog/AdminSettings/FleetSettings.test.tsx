import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { testQueryClient } from "@/test/query-client";

import { FleetSettings } from "./FleetSettings";

const gib = (n: number) => n * 1024;
const capacity = (cpu: number, memoryGiB: number, gpus = 0): Schemas["FleetCapacity"] => ({
  cpu_millicores: cpu * 1000,
  memory_mib: gib(memoryGiB),
  gpu_count: gpus,
});

const onDemand: Schemas["FleetMarket"] = {
  preemptible: false,
  gpu_type: "",
  warm_free: capacity(4, 16),
  warm_target: capacity(2, 4),
  reserve_ready: capacity(4, 16),
  reserve_target: capacity(6, 12),
  allocated: capacity(0, 0),
  states: [
    { state: "image_saved", machines: 1, capacity: capacity(4, 16), allocated: capacity(0, 0) },
    { state: "serving", machines: 1, capacity: capacity(4, 16), allocated: capacity(0, 0) },
  ],
  reason: "no approved offer",
};

function node(id: string, state: Schemas["FleetState"]): Schemas["FleetNode"] {
  return {
    id,
    instance_id: `i-${id}`,
    provider: "aws",
    region: "us-east-2",
    instance_type: "m7i.xlarge",
    preemptible: false,
    gpu_type: "",
    state,
    capacity: capacity(4, 16),
    allocated: capacity(0, 0),
    containers: 0,
    ready: false,
  };
}

function serve(summary: Schemas["FleetSummary"], nodes: Schemas["FleetNode"][] = []) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL((input as Request).url);
    if (url.pathname === "/v1/fleet/nodes") {
      return Response.json({ nodes, observed_at: summary.observed_at });
    }
    return Response.json(summary);
  });
  render(
    <QueryClientProvider client={testQueryClient()}>
      <FleetSettings />
    </QueryClientProvider>,
  );
}

const observed = "2026-10-02T12:00:00Z";

it("shows each market's published targets and its states when expanded", async () => {
  const expires = new Date(Date.now() + 5 * 60_000).toISOString();
  serve({
    observed_at: observed,
    plan: { generated_at: observed, expires_at: expires, markets: [onDemand] },
  });

  const market = await screen.findByRole("button", { name: "On-demand CPU" });
  const row = market.closest("tr") as HTMLElement;
  expect(within(row).getAllByText("4 CPU · 16 GiB")).toHaveLength(2);
  expect(within(row).getByText("6 CPU · 12 GiB")).toBeVisible();
  expect(within(row).getByText("2 CPU · 4 GiB")).toBeVisible();

  fireEvent.click(market);
  expect(market).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("Hibernated")).toBeVisible();
  expect(screen.getByText("Serving")).toBeVisible();
  expect(screen.getByText("no approved offer")).toBeVisible();
});

it("says capacity data is unavailable without a plan", async () => {
  serve({ observed_at: observed });
  expect(await screen.findByRole("status")).toHaveTextContent(
    "Capacity data unavailable. Refresh to try again.",
  );
});

it("says capacity data expired once the plan's expiry passes", async () => {
  serve({
    observed_at: observed,
    plan: {
      generated_at: observed,
      expires_at: new Date(Date.now() - 1000).toISOString(),
      markets: [onDemand],
    },
  });
  expect(await screen.findByRole("status")).toHaveTextContent(
    "Capacity data expired. Refresh to update.",
  );
  expect(screen.queryByRole("button", { name: "On-demand CPU" })).not.toBeInTheDocument();
});

it("names every reserve state on the Nodes tab", async () => {
  const states: [Schemas["FleetState"], string][] = [
    ["preparing", "Preparing reserve"],
    ["stopping", "Stopping"],
    ["stopped", "Stopped"],
    ["hibernate_unverified", "Hibernation unverified"],
    ["image_saved", "Hibernated"],
    ["starting", "Starting"],
  ];
  serve(
    { observed_at: observed },
    states.map(([state], i) => node(`n${i}`, state)),
  );

  const tab = screen.getByRole("tab", { name: "Nodes" });
  fireEvent.mouseDown(tab, { button: 0 });
  for (const [i, [, label]] of states.entries()) {
    const row = (await screen.findByText(`i-n${i}`)).closest("tr") as HTMLElement;
    expect(within(row).getByText(label)).toBeVisible();
  }
  expect(screen.getByText("Hibernation unverified")).toHaveClass("text-warning");
});

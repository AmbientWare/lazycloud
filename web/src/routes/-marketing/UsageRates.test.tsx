import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { computeGroups, RateList } from "./UsageRates";

function fleetRate(gpuType: string | undefined, gpuCardHour: number): Schemas["ComputeRate"] {
  return {
    billing_owner: "platform_fleet",
    gpu_type: gpuType,
    nanos_per_container_hour: 0,
    nanos_per_cpu_core_hour: 22_000_000,
    nanos_per_memory_gib_hour: 7_500_000,
    nanos_per_gpu_card_hour: gpuCardHour,
  };
}

function gpuRate(gpuType: string, enabled: boolean): Schemas["GpuRate"] {
  return {
    gpu_type: gpuType,
    enabled,
    nanos_per_card_hour: { platform_fleet: 0, connected_cloud: 0, self_hosted: 0 },
  };
}

it("prices a model that is not offered yet and marks it coming soon", () => {
  const placement: Schemas["PlacementRate"] = {
    rate_class: "auto",
    name: "Automatic",
    effective_at: "2026-09-10T00:00:00Z",
    pinned: false,
    preemptible: true,
    cpu_memory_multiplier: 1,
    gpu_multiplier: 1,
    compute_rates: [
      fleetRate(undefined, 0),
      fleetRate("T4", 350_000_000),
      fleetRate("H100", 2_250_000_000),
    ],
  };
  render(
    <RateList
      groups={computeGroups(placement, [gpuRate("T4", true), gpuRate("H100", false)], "hour")}
    />,
  );

  const h100 = screen.getByText("H100").parentElement;
  expect(h100).toHaveTextContent("Coming soon");
  expect(h100).toHaveTextContent("$2.25");
  const t4 = screen.getByText("T4").parentElement;
  expect(t4).toHaveTextContent("$0.35");
  expect(t4).not.toHaveTextContent("Coming soon");
});

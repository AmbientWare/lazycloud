import { testQueryClient } from "@/test/query-client";
import { QueryObserver } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { workloadNotFound, workloadQueryOptions } from "./deployments";

afterEach(() => vi.unstubAllGlobals());

const detail: Schemas["WorkloadDetail"] = {
  workload: {
    id: "workload-1",
    app: "journey",
    name: "greet",
    kind: "function",
    state: "active",
    version: 2,
    release_id: "release-2",
    running_containers: 1,
    created_at: "2026-07-10T12:00:00Z",
    deployed_at: "2026-07-10T12:00:00Z",
  },
  release: {
    id: "release-2",
    name: "greet",
    version: 2,
    created_at: "2026-07-10T12:00:00Z",
    spec: {
      kind: "function",
      name: "greet",
      handler: "journey:greet",
      source: { sha256: "0".repeat(64) },
      image: { python_version: "3.12" },
      resources: { cpu_millis: 250, memory_mib: 256 },
      cron: "0 * * * *",
    },
  },
  schedule: { cron: "0 * * * *", timezone: "UTC", next_run_at: "2026-07-10T13:00:00Z" },
};

it("reads the workload at its address, and reads it again once a missing one is deployed", async () => {
  let deployed = false;
  const requests: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async (input) => {
      const url = new URL((input as Request).url);
      requests.push(url);
      return deployed
        ? Response.json(detail)
        : Response.json({ code: "not_found", message: "no such workload" }, { status: 404 });
    }),
  );
  const client = testQueryClient();
  const observer = new QueryObserver(
    client,
    workloadQueryOptions("dev", { app: "journey", kind: "function", name: "greet" }),
  );
  const unsubscribe = observer.subscribe(() => {});
  try {
    await vi.waitFor(() => expect(workloadNotFound(observer.getCurrentResult().error)).toBe(true));
    // Absence is an answer, not a failure to retry.
    expect(requests.map((url) => url.pathname)).toEqual([
      "/v1/workspaces/dev/apps/journey/workloads/function/greet",
    ]);

    deployed = true;
    const refetched = await observer.refetch();
    expect(refetched.data).toEqual(detail);
  } finally {
    unsubscribe();
  }
});

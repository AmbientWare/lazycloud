import { testQueryClient } from "@/test/query-client";
import { QueryObserver } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { WorkloadNotFoundError, workloadQueryOptions } from "./deployments";

afterEach(() => vi.unstubAllGlobals());

const greet: Schemas["DeployedWorkload"] = {
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
};

const release: Schemas["Release"] = {
  id: "release-2",
  function: "greet",
  version: 2,
  created_at: "2026-07-10T12:00:00Z",
  spec: {
    name: "greet",
    handler: "journey:greet",
    source: { kind: "inline" } as unknown as Schemas["SourceRef"],
    image: {} as Schemas["ImageSpec"],
    resources: { cpu_millis: 250, memory_mib: 256 },
    cron: "0 * * * *",
  },
};

const schedule: Schemas["Schedule"] = {
  cron: "0 * * * *",
  timezone: "UTC",
  next_run_at: "2026-07-10T13:00:00Z",
};

function stubApi(deployed: () => Schemas["DeployedWorkload"][]) {
  const requests: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async (input) => {
      const url = new URL((input as Request).url);
      requests.push(url);
      if (url.pathname.endsWith("/deployments")) {
        const app = url.searchParams.get("app");
        const name = url.searchParams.get("name");
        return Response.json({
          deployments: deployed().filter((item) => item.app === app && item.name === name),
        });
      }
      if (url.pathname.endsWith("/functions/greet")) {
        return Response.json({
          name: "greet",
          app: "journey",
          state: "active",
          active_release: release,
          schedule,
        });
      }
      return Response.json({ message: "unexpected request" }, { status: 500 });
    }),
  );
  return requests;
}

it("asks for exactly the named workload and reports one the app does not deploy", async () => {
  let deployed: Schemas["DeployedWorkload"][] = [];
  const requests = stubApi(() => deployed);
  const client = testQueryClient({ defaultOptions: { queries: { retry: false } } });
  const observer = new QueryObserver(
    client,
    workloadQueryOptions("dev", "journey", "function", "greet"),
  );
  const unsubscribe = observer.subscribe(() => {});
  try {
    await vi.waitFor(() =>
      expect(observer.getCurrentResult().error).toBeInstanceOf(WorkloadNotFoundError),
    );
    const listing = requests.find((url) => url.pathname.endsWith("/deployments"));
    expect(listing?.searchParams.get("app")).toBe("journey");
    expect(listing?.searchParams.get("name")).toBe("greet");

    // Deployed after the first read: the next fetch reads the server again.
    deployed = [greet];
    const refetched = await observer.refetch();
    expect(refetched.data?.deployment.id).toBe("workload-1");
  } finally {
    unsubscribe();
  }
});

it("does not take a workload of another kind with the same name", async () => {
  stubApi(() => [greet]);
  await expect(
    testQueryClient().fetchQuery(workloadQueryOptions("dev", "journey", "endpoint", "greet")),
  ).rejects.toBeInstanceOf(WorkloadNotFoundError);
});

it("reads a pod's definition, and the role that makes it a devbox, from the function read", async () => {
  stubApi(() => [{ ...greet, kind: "pod", role: "devbox" }]);
  const workload = await testQueryClient().fetchQuery(
    workloadQueryOptions("dev", "journey", "pod", "greet"),
  );
  expect(workload.deployment.role).toBe("devbox");
  expect(workload.release.id).toBe("release-2");
  expect(workload.http).toBeNull();
});

it("reads a scheduled function's runs from its own read, whatever the workspace holds", async () => {
  const requests = stubApi(() => [greet]);
  const workload = await testQueryClient().fetchQuery(
    workloadQueryOptions("dev", "journey", "function", "greet"),
  );
  expect(workload.schedule).toEqual(schedule);
  expect(workload.release.spec.cron).toBe("0 * * * *");
  expect(requests.some((url) => url.pathname.endsWith("/schedules"))).toBe(false);
});

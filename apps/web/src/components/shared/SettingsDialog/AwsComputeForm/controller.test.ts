import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AwsComputeConfiguration, AwsConnection } from "@/lib/api/schemas";
import { accountComputeQueryKeys } from "@/lib/queries/compute";

import { useAwsComputeController } from "./controller";

afterEach(() => vi.restoreAllMocks());

describe("AWS compute configuration controller", () => {
  it("replaces a clean draft and field-rebases a dirty draft for explicit review", async () => {
    let authority = connection(1);
    mockConnectionApi(() => authority);
    const queryClient = testQueryClient();
    const { result } = renderController(queryClient);
    await waitFor(() => expect(result.current.draft?.maxCpuInstances).toBe(10));

    authority = connection(2, { maxGpuInstances: 7 });
    act(() => {
      queryClient.setQueryData(accountComputeQueryKeys.awsConnection(), authority);
    });
    await waitFor(() => expect(result.current.draft?.maxGpuInstances).toBe(7));
    expect(result.current.isDirty).toBe(false);

    act(() => {
      result.current.updateField({ field: "maxCpuInstances", value: 42 });
      result.current.updateField({ field: "allowedInstanceTypes", value: "g5.xlarge" });
    });
    authority = connection(3, { maxCpuInstances: 12, maxGpuInstances: 9 });
    act(() => {
      queryClient.setQueryData(accountComputeQueryKeys.awsConnection(), authority);
    });

    await waitFor(() => expect(result.current.requiresReview).toBe(true));
    expect(result.current.draft).toMatchObject({
      maxCpuInstances: 42,
      maxGpuInstances: 9,
      allowedInstanceTypes: "g5.xlarge",
    });
    expect(result.current.configuration?.revision).toBe(3);
    expect(result.current.dirtyFields).toEqual(["maxCpuInstances", "allowedInstanceTypes"]);
    expect(result.current.canSave).toBe(false);

    act(() => result.current.review());
    expect(result.current.requiresReview).toBe(false);
    expect(result.current.canSave).toBe(true);
  });

  it("treats an ordered-array reorder as one atomic dirty field during rebase", async () => {
    const queryClient = testQueryClient();
    mockConnectionApi(() => connection(1));
    const { result } = renderController(queryClient);
    await waitFor(() => expect(result.current.draft?.allowedRegions).toHaveLength(2));

    act(() => {
      result.current.updateField({
        field: "allowedRegions",
        value: ["us-west-2", "us-east-1"],
      });
    });
    expect(result.current.dirtyFields).toEqual(["allowedRegions"]);

    act(() => {
      queryClient.setQueryData(
        accountComputeQueryKeys.awsConnection(),
        connection(2, { maxGpuInstances: 5 }),
      );
    });
    await waitFor(() => expect(result.current.requiresReview).toBe(true));
    expect(result.current.draft?.allowedRegions).toEqual(["us-west-2", "us-east-1"]);
    expect(result.current.draft?.maxGpuInstances).toBe(5);
    expect(result.current.dirtyFields).toEqual(["allowedRegions"]);
  });

  it("freezes one exact request and replaces cache and draft only with the response", async () => {
    const pending = deferred<Response>();
    const requests: RequestInit[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method === "PUT") {
        requests.push(init);
        return pending.promise;
      }
      return jsonResponse({ connection: connection(4) });
    });
    const queryClient = testQueryClient();
    const authoritative = connection(4);
    queryClient.setQueryData(accountComputeQueryKeys.awsConnection(), authoritative);
    const { result } = renderController(queryClient);
    await waitFor(() => expect(result.current.draft?.maxCpuInstances).toBe(10));

    act(() => result.current.updateField({ field: "maxCpuInstances", value: 44 }));
    act(() => {
      result.current.save();
      result.current.save();
    });

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]?.body).toBe(
      JSON.stringify({
        expected_revision: 4,
        compute: { ...authoritative.compute, max_cpu_instances: 44 },
      }),
    );
    expect(queryClient.getQueryData(accountComputeQueryKeys.awsConnection())).toEqual(
      authoritative,
    );

    const accepted = connection(5, { maxCpuInstances: 44, maxGpuInstances: 8 });
    pending.resolve(jsonResponse(accepted));
    await waitFor(() => expect(result.current.isSaved).toBe(true));

    expect(result.current.draft).toMatchObject({ maxCpuInstances: 44, maxGpuInstances: 8 });
    expect(queryClient.getQueryData(accountComputeQueryKeys.awsConnection())).toEqual(accepted);
  });

  it("keeps an ordinary failure retryable", async () => {
    let updates = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method !== "PUT") return jsonResponse({ connection: connection(1) });
      updates += 1;
      return updates === 1
        ? jsonResponse({ detail: "compute service unavailable" }, 503)
        : jsonResponse(connection(2, { maxCpuInstances: 24 }));
    });
    const { result } = renderController(testQueryClient());
    await waitFor(() => expect(result.current.draft).not.toBeNull());
    act(() => result.current.updateField({ field: "maxCpuInstances", value: 24 }));

    act(() => result.current.save());
    await waitFor(() =>
      expect(result.current.saveError?.message).toBe("compute service unavailable"),
    );
    expect(result.current.canSave).toBe(true);

    act(() => result.current.save());
    await waitFor(() => expect(result.current.isSaved).toBe(true));
    expect(updates).toBe(2);
  });

  it("loads authority after a conflict and waits for review before one explicit retry", async () => {
    let authority = connection(1);
    const updateBodies: BodyInit[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method !== "PUT") return jsonResponse({ connection: authority });
      if (init.body) updateBodies.push(init.body);
      if (updateBodies.length === 1) {
        authority = connection(2, { maxCpuInstances: 12, maxGpuInstances: 7 });
        return jsonResponse({ detail: "compute configuration revision changed" }, 409);
      }
      return jsonResponse(connection(3, { maxCpuInstances: 30, maxGpuInstances: 7 }));
    });
    const { result } = renderController(testQueryClient());
    await waitFor(() => expect(result.current.draft).not.toBeNull());
    act(() => result.current.updateField({ field: "maxCpuInstances", value: 30 }));

    act(() => result.current.save());
    await waitFor(() => expect(result.current.requiresReview).toBe(true));

    expect(updateBodies).toHaveLength(1);
    expect(updateBodies[0]).toContain('"expected_revision":1');
    expect(result.current.draft).toMatchObject({ maxCpuInstances: 30, maxGpuInstances: 7 });
    expect(result.current.configuration?.revision).toBe(2);
    expect(result.current.canSave).toBe(false);

    act(() => result.current.review());
    act(() => result.current.save());
    await waitFor(() => expect(result.current.isSaved).toBe(true));

    expect(updateBodies).toHaveLength(2);
    expect(updateBodies[1]).toContain('"expected_revision":2');
  });

  it("preserves the draft and disables save until failed conflict recovery reloads", async () => {
    let getCount = 0;
    let recoveryCanSucceed = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method === "PUT") {
        return jsonResponse({ detail: "compute configuration revision changed" }, 409);
      }
      getCount += 1;
      if (getCount > 1 && !recoveryCanSucceed) {
        return jsonResponse({ detail: "could not load the AWS connection" }, 503);
      }
      return jsonResponse({
        connection: getCount === 1 ? connection(1) : connection(2, { maxGpuInstances: 6 }),
      });
    });
    const { result } = renderController(testQueryClient());
    await waitFor(() => expect(result.current.draft).not.toBeNull());
    act(() => result.current.updateField({ field: "maxCpuInstances", value: 35 }));

    act(() => result.current.save());
    await waitFor(() => expect(result.current.recoveryFailed).toBe(true));
    expect(result.current.draft?.maxCpuInstances).toBe(35);
    expect(result.current.canSave).toBe(false);
    expect(result.current.saveError?.message).toBe("could not load the AWS connection");

    recoveryCanSucceed = true;
    act(() => result.current.retryLoad());
    await waitFor(() => expect(result.current.requiresReview).toBe(true));
    expect(result.current.draft).toMatchObject({ maxCpuInstances: 35, maxGpuInstances: 6 });
    expect(result.current.configuration?.revision).toBe(2);
  });
});

function renderController(queryClient: QueryClient) {
  return renderHook(() => useAwsComputeController(), {
    wrapper: ({ children }: PropsWithChildren) =>
      createElement(QueryClientProvider, { client: queryClient }, children),
  });
}

function testQueryClient() {
  return new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false },
    },
  });
}

function mockConnectionApi(authority: () => AwsConnection) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
    jsonResponse({ connection: authority() }),
  );
}

function compute(
  revision: number,
  overrides: { maxCpuInstances?: number; maxGpuInstances?: number },
): AwsComputeConfiguration {
  return {
    revision,
    default_region: "us-east-1",
    default_instance_type: "i4i.xlarge",
    initial_cpu_workers: 1,
    min_cpu_workers: 1,
    max_cpu_instances: overrides.maxCpuInstances ?? 10,
    max_gpu_instances: overrides.maxGpuInstances ?? 2,
    min_free_cpu_millicores: 1_000,
    min_free_memory_mib: 1_024,
    allowed_regions: ["us-east-1", "us-west-2"],
    allowed_instance_types: [],
    idle_timeout_seconds: 300,
    root_volume_gib: 200,
  };
}

function connection(
  revision: number,
  overrides: { maxCpuInstances?: number; maxGpuInstances?: number } = {},
): AwsConnection {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    account_id: "123456789012",
    phase: "ready",
    active_authorization: null,
    pending_authorization: null,
    retiring_authorization: null,
    revision,
    compute: compute(revision, overrides),
    hosts_workloads: true,
    can_manage_existing_capacity: true,
    available_actions: ["reconnect", "remove"],
    detail: "AWS compute is available for your workspaces.",
    customer_action: null,
    next_retry_at: null,
    created_at: "2026-07-14T12:00:00Z",
    updated_at: `2026-07-14T12:00:0${revision}Z`,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

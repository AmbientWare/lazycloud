import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceComputePolicy } from "@/lib/api/schemas";
import { computeQueryKeys } from "@/lib/queries/compute";

import { useComputePolicyController } from "./controller";

afterEach(() => vi.restoreAllMocks());

describe("compute policy controller", () => {
  it("replaces a clean draft and field-rebases a dirty draft for explicit review", async () => {
    let authority = policy(1);
    mockPolicyApi(() => authority);
    const queryClient = testQueryClient();
    const { result } = renderController(queryClient);
    await waitFor(() => expect(result.current.draft?.maxCpuInstances).toBe(10));

    authority = policy(2, { maxGpuInstances: 7 });
    act(() => {
      queryClient.setQueryData(computeQueryKeys.policy("workspace-1"), authority);
    });
    await waitFor(() => expect(result.current.draft?.maxGpuInstances).toBe(7));
    expect(result.current.isDirty).toBe(false);

    act(() => {
      result.current.updateField({ field: "maxCpuInstances", value: 42 });
      result.current.updateField({ field: "allowedInstanceTypes", value: "g5.xlarge" });
    });
    authority = policy(3, { maxCpuInstances: 12, maxGpuInstances: 9 });
    act(() => {
      queryClient.setQueryData(computeQueryKeys.policy("workspace-1"), authority);
    });

    await waitFor(() => expect(result.current.requiresReview).toBe(true));
    expect(result.current.draft).toMatchObject({
      maxCpuInstances: 42,
      maxGpuInstances: 9,
      allowedInstanceTypes: "g5.xlarge",
    });
    expect(result.current.policy?.revision).toBe(3);
    expect(result.current.dirtyFields).toEqual([
      "maxCpuInstances",
      "allowedInstanceTypes",
    ]);
    expect(result.current.canSave).toBe(false);

    act(() => result.current.review());
    expect(result.current.requiresReview).toBe(false);
    expect(result.current.canSave).toBe(true);
  });

  it("treats an ordered-array reorder as one atomic dirty field during rebase", async () => {
    const queryClient = testQueryClient();
    mockPolicyApi(() => policy(1));
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
        computeQueryKeys.policy("workspace-1"),
        policy(2, { maxGpuInstances: 5 }),
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
      return jsonResponse(policy(4));
    });
    const queryClient = testQueryClient();
    const authoritative = policy(4);
    queryClient.setQueryData(computeQueryKeys.policy("workspace-1"), authoritative);
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
        default_placement: "managed",
        aws: {
          ...authoritative.aws,
          max_cpu_instances: 44,
        },
      }),
    );
    expect(queryClient.getQueryData(computeQueryKeys.policy("workspace-1"))).toEqual(
      authoritative,
    );

    const accepted = policy(5, { maxCpuInstances: 44, maxGpuInstances: 8 });
    pending.resolve(jsonResponse(accepted));
    await waitFor(() => expect(result.current.isSaved).toBe(true));

    expect(result.current.draft).toMatchObject({
      maxCpuInstances: 44,
      maxGpuInstances: 8,
    });
    expect(queryClient.getQueryData(computeQueryKeys.policy("workspace-1"))).toEqual(
      accepted,
    );
  });

  it("keeps an ordinary failure retryable", async () => {
    let updates = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method !== "PUT") return jsonResponse(policy(1));
      updates += 1;
      return updates === 1
        ? jsonResponse({ detail: "policy service unavailable" }, 503)
        : jsonResponse(policy(2, { maxCpuInstances: 24 }));
    });
    const { result } = renderController(testQueryClient());
    await waitFor(() => expect(result.current.draft).not.toBeNull());
    act(() => result.current.updateField({ field: "maxCpuInstances", value: 24 }));

    act(() => result.current.save());
    await waitFor(() => expect(result.current.saveError?.message).toBe("policy service unavailable"));
    expect(result.current.canSave).toBe(true);

    act(() => result.current.save());
    await waitFor(() => expect(result.current.isSaved).toBe(true));
    expect(updates).toBe(2);
  });

  it("loads authority after a conflict and waits for review before one explicit retry", async () => {
    let authority = policy(1);
    const updateBodies: BodyInit[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method !== "PUT") return jsonResponse(authority);
      if (init.body) updateBodies.push(init.body);
      if (updateBodies.length === 1) {
        authority = policy(2, { maxCpuInstances: 12, maxGpuInstances: 7 });
        return jsonResponse({ detail: "policy revision changed" }, 409);
      }
      return jsonResponse(policy(3, { maxCpuInstances: 30, maxGpuInstances: 7 }));
    });
    const { result } = renderController(testQueryClient());
    await waitFor(() => expect(result.current.draft).not.toBeNull());
    act(() => result.current.updateField({ field: "maxCpuInstances", value: 30 }));

    act(() => result.current.save());
    await waitFor(() => expect(result.current.requiresReview).toBe(true));

    expect(updateBodies).toHaveLength(1);
    expect(updateBodies[0]).toContain('"expected_revision":1');
    expect(result.current.draft).toMatchObject({
      maxCpuInstances: 30,
      maxGpuInstances: 7,
    });
    expect(result.current.policy?.revision).toBe(2);
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
        return jsonResponse({ detail: "policy revision changed" }, 409);
      }
      getCount += 1;
      if (getCount > 1 && !recoveryCanSucceed) {
        return jsonResponse({ detail: "could not load policy" }, 503);
      }
      return jsonResponse(getCount === 1 ? policy(1) : policy(2, { maxGpuInstances: 6 }));
    });
    const { result } = renderController(testQueryClient());
    await waitFor(() => expect(result.current.draft).not.toBeNull());
    act(() => result.current.updateField({ field: "maxCpuInstances", value: 35 }));

    act(() => result.current.save());
    await waitFor(() => expect(result.current.recoveryFailed).toBe(true));
    expect(result.current.draft?.maxCpuInstances).toBe(35);
    expect(result.current.canSave).toBe(false);
    expect(result.current.saveError?.message).toBe("could not load policy");

    recoveryCanSucceed = true;
    act(() => result.current.retryLoad());
    await waitFor(() => expect(result.current.requiresReview).toBe(true));
    expect(result.current.draft).toMatchObject({
      maxCpuInstances: 35,
      maxGpuInstances: 6,
    });
    expect(result.current.policy?.revision).toBe(2);
  });

  it("drops another workspace's base, draft, and errors on workspace change", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      jsonResponse(String(input).includes("workspace-2") ? policy(8, { maxCpuInstances: 80 }) : policy(1)),
    );
    const queryClient = testQueryClient();
    const { result, rerender } = renderHook(
      ({ workspaceId }: { workspaceId: string }) =>
        useComputePolicyController(workspaceId),
      {
        initialProps: { workspaceId: "workspace-1" },
        wrapper: controllerWrapper(queryClient),
      },
    );
    await waitFor(() => expect(result.current.draft?.maxCpuInstances).toBe(10));
    act(() => result.current.updateField({ field: "maxCpuInstances", value: 77 }));
    expect(result.current.isDirty).toBe(true);

    rerender({ workspaceId: "workspace-2" });
    expect(result.current.draft?.maxCpuInstances).not.toBe(77);
    await waitFor(() => expect(result.current.draft?.maxCpuInstances).toBe(80));
    expect(result.current.policy?.revision).toBe(8);
    expect(result.current.isDirty).toBe(false);
    expect(result.current.saveError).toBeNull();

    rerender({ workspaceId: "workspace-1" });
    await waitFor(() => expect(result.current.draft?.maxCpuInstances).toBe(10));
    expect(result.current.isDirty).toBe(false);
  });
});

function renderController(queryClient: QueryClient) {
  return renderHook(() => useComputePolicyController("workspace-1"), {
    wrapper: controllerWrapper(queryClient),
  });
}

function controllerWrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function testQueryClient() {
  return new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false },
    },
  });
}

function mockPolicyApi(authority: () => WorkspaceComputePolicy) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => jsonResponse(authority()));
}

function policy(
  revision: number,
  overrides: {
    maxCpuInstances?: number;
    maxGpuInstances?: number;
  } = {},
): WorkspaceComputePolicy {
  return {
    revision,
    default_placement: "managed",
    aws: {
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
    },
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

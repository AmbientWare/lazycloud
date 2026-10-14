import { testQueryClient } from "@/test/query-client";
import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

import { useAwsConnectionController } from "./controller";

type Sent = { method: string; path: string; body: unknown };

/** Records each call and answers it with the next queued response for its method and path. */
function serve(answers: Record<string, (() => Response | Promise<Response>)[]>): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal("fetch", async (request: Request) => {
    const path = new URL(request.url).pathname;
    const text = await request.text();
    sent.push({ method: request.method, path, body: text ? JSON.parse(text) : undefined });
    const answer = answers[`${request.method} ${path}`]?.shift();
    if (!answer) throw new Error(`Unexpected request: ${request.method} ${path}`);
    return answer();
  });
  return sent;
}

const unavailable = () =>
  Response.json({ code: "unavailable", message: "AWS is unavailable" }, { status: 503 });

describe("AWS connection controller", () => {
  it("connects the trimmed account and keeps a failed attempt visible for retry", async () => {
    const sent = serve({
      "POST /v1/aws-connection": [
        unavailable,
        () => Response.json({ connection: awsConnection(), authorization: {} }, { status: 201 }),
      ],
    });
    const { result } = renderController(testQueryClient(), vi.fn());

    await runAndSettle(result, () => result.current.create(" 123456789012 "));
    expect(result.current.createError?.message).toBe("AWS is unavailable");
    expect(result.current.recoveryError).toBeNull();

    await runAndSettle(result, () => result.current.create("123456789012"));
    expect(result.current.createError).toBeNull();
    expect(sent).toEqual([
      { method: "POST", path: "/v1/aws-connection", body: { account_id: "123456789012" } },
      { method: "POST", path: "/v1/aws-connection", body: { account_id: "123456789012" } },
    ]);
  });

  it("runs one recovery action at a time and keeps a failed one retryable under its own error", async () => {
    const validation = deferred<Response>();
    const sent = serve({
      "POST /v1/aws-connection/validate": [unavailable, () => validation.promise],
    });
    const { result } = renderController(testQueryClient(), vi.fn());

    await runAndSettle(result, () => result.current.validate());
    expect(result.current.recoveryError?.message).toBe("AWS is unavailable");
    expect(result.current.createError).toBeNull();
    expect(result.current.removalError).toBeNull();

    act(() => {
      result.current.validate();
      result.current.retry();
      result.current.reconnect();
    });
    expect(result.current.activeAction).toBe("validate");
    validation.resolve(Response.json(awsConnection()));
    await waitFor(() => expect(result.current.activeAction).toBeNull());

    expect(result.current.recoveryError).toBeNull();
    expect(sent.map((call) => `${call.method} ${call.path}`)).toEqual([
      "POST /v1/aws-connection/validate",
      "POST /v1/aws-connection/validate",
    ]);
  });

  it("closes both dialogs and invalidates every AWS projection after accepted removal", async () => {
    const queryClient = testQueryClient();
    const onClose = vi.fn();
    const projections = [
      accountQueryKeys.compute.awsConnection(),
      accountQueryKeys.compute.instances(),
      accountQueryKeys.compute.machines(),
    ];
    for (const queryKey of projections) {
      queryClient.setQueryData(queryKey, { stale: true });
    }
    serve({
      "DELETE /v1/aws-connection": [
        () => Response.json({ connection: awsConnection("disconnect_draining") }, { status: 202 }),
      ],
    });
    const { result } = renderController(queryClient, onClose);

    act(() => result.current.setRemoveOpen(true));
    await runAndSettle(result, () => result.current.remove());

    expect(result.current.removeOpen).toBe(false);
    expect(onClose).toHaveBeenCalledOnce();
    expect(
      projections.map((queryKey) => queryClient.getQueryState(queryKey)?.isInvalidated),
    ).toEqual([true, true, true]);
  });
});

function renderController(queryClient: QueryClient, onClose: () => void) {
  return renderHook(() => useAwsConnectionController({ onClose }), {
    wrapper: ({ children }: PropsWithChildren) =>
      createElement(QueryClientProvider, { client: queryClient }, children),
  });
}

async function runAndSettle(
  result: ReturnType<typeof renderController>["result"],
  run: () => void,
) {
  act(run);
  await waitFor(() => expect(result.current.activeAction).toBeNull());
}

function deferred<T>() {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

function awsConnection(phase: "ready" | "disconnect_draining" = "ready"): Schemas["AwsConnection"] {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    account_id: "123456789012",
    phase,
    active_authorization: {
      generation: 1,
      authorization_mode: "managed_stack",
      managed_authorization: {
        stack_name: "compute-connection-test-g1",
        region: "us-east-1",
        generation: 1,
        stack_id:
          "arn:aws:cloudformation:us-east-1:123456789012:stack/compute-connection-test-g1/stack-id",
        template_version: "test.v1",
        template_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      },
      phase: "ready",
      last_validation_started_at: "2026-07-20T00:00:00Z",
      last_validated_at: "2026-07-20T00:00:01Z",
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:01Z",
    },
    revision: 1,
    hosts_workloads: phase === "ready",
    can_manage_existing_capacity: phase === "ready",
    available_actions: phase === "ready" ? ["validate", "reconnect", "remove"] : [],
    detail: "AWS connection state",
    created_at: "2026-07-20T00:00:00Z",
    updated_at: "2026-07-20T00:00:01Z",
  };
}

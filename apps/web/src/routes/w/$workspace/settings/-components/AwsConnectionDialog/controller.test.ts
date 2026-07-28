import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AwsConnection } from "@/lib/api/schemas";
import {
  computeQueryKeys,
  createAwsConnection,
  reconnectAwsConnection,
  removeAwsConnection,
  retryAwsConnection,
  validateAwsConnection,
} from "@/lib/queries/compute";

import { type AwsAuthorizationPopup, useAwsConnectionController } from "./controller";

vi.mock("@/lib/queries/compute", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/queries/compute")>();
  return {
    ...actual,
    cancelAwsConnectionReconnect: vi.fn(),
    createAwsConnection: vi.fn(),
    reconnectAwsConnection: vi.fn(),
    removeAwsConnection: vi.fn(),
    retryAwsConnection: vi.fn(),
    validateAwsConnection: vi.fn(),
  };
});

const createMock = vi.mocked(createAwsConnection);
const validateMock = vi.mocked(validateAwsConnection);
const reconnectMock = vi.mocked(reconnectAwsConnection);
const retryMock = vi.mocked(retryAwsConnection);
const removeMock = vi.mocked(removeAwsConnection);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("AWS connection controller", () => {
  it("closes failed authorization popups and keeps the error visible for retry", async () => {
    const queryClient = testQueryClient();
    const popup = popupFixture();
    const failure = new Error("AWS is unavailable");
    createMock.mockRejectedValueOnce(failure).mockResolvedValueOnce({
      connection: awsConnection(),
      authorization: { url: null },
    });
    const { result } = renderController(queryClient, vi.fn(), () => popup.value);

    act(() => result.current.create("123456789012"));
    await waitFor(() => expect(result.current.activeAction).toBeNull());

    expect(popup.close).toHaveBeenCalledOnce();
    expect(result.current.createError).toBe(failure);
    expect(result.current.recoveryError).toBeNull();

    act(() => result.current.create("123456789012"));
    await waitFor(() => expect(result.current.activeAction).toBeNull());

    expect(createMock).toHaveBeenCalledTimes(2);
    expect(popup.close).toHaveBeenCalledTimes(2);
    expect(result.current.createError).toBeNull();
  });

  it("allows only one recovery action at a time", async () => {
    const pendingValidation = deferred<ReturnType<typeof awsConnection>>();
    validateMock.mockReturnValue(pendingValidation.promise);
    retryMock.mockResolvedValue(awsConnection());
    const openAuthorizationPopup = vi.fn(() => popupFixture().value);
    const { result } = renderController(testQueryClient(), vi.fn(), openAuthorizationPopup);

    act(() => {
      result.current.validate();
      result.current.retry();
      result.current.reconnect();
    });

    expect(result.current.activeAction).toBe("validate");
    await waitFor(() => expect(validateMock).toHaveBeenCalledOnce());
    expect(retryMock).not.toHaveBeenCalled();
    expect(reconnectMock).not.toHaveBeenCalled();
    expect(openAuthorizationPopup).not.toHaveBeenCalled();

    pendingValidation.resolve(awsConnection());
    await waitFor(() => expect(result.current.activeAction).toBeNull());
  });

  it("keeps failed recovery actions retryable and scopes their error", async () => {
    const failure = new Error("validation failed");
    validateMock.mockRejectedValueOnce(failure).mockResolvedValueOnce(awsConnection());
    const { result } = renderController(testQueryClient(), vi.fn(), () => popupFixture().value);

    act(() => result.current.validate());
    await waitFor(() => expect(result.current.activeAction).toBeNull());

    expect(result.current.recoveryError).toBe(failure);
    expect(result.current.createError).toBeNull();
    expect(result.current.removalError).toBeNull();

    act(() => result.current.validate());
    await waitFor(() => expect(result.current.activeAction).toBeNull());

    expect(validateMock).toHaveBeenCalledTimes(2);
    expect(result.current.recoveryError).toBeNull();
  });

  it("closes both dialogs and invalidates every AWS projection after accepted removal", async () => {
    const queryClient = testQueryClient();
    const onClose = vi.fn();
    const projections = [
      computeQueryKeys.awsConnection("workspace-1"),
      computeQueryKeys.policy("workspace-1"),
      computeQueryKeys.instances("workspace-1"),
    ];
    for (const queryKey of projections) {
      queryClient.setQueryData(queryKey, { stale: true });
    }
    removeMock.mockResolvedValue(awsConnection("disconnect_draining"));
    const { result } = renderController(queryClient, onClose, () => popupFixture().value);

    act(() => result.current.setRemoveOpen(true));
    expect(result.current.removeOpen).toBe(true);
    await runAndSettle(result, result.current.remove);

    expect(result.current.removeOpen).toBe(false);
    expect(onClose).toHaveBeenCalledOnce();
    await waitFor(() =>
      expect(
        projections.map((queryKey) => queryClient.getQueryState(queryKey)?.isInvalidated),
      ).toEqual([true, true, true]),
    );
  });
});

function renderController(
  queryClient: QueryClient,
  onClose: () => void,
  openAuthorizationPopup: () => AwsAuthorizationPopup,
) {
  return renderHook(
    () =>
      useAwsConnectionController({
        workspaceId: "workspace-1",
        onClose,
        openAuthorizationPopup,
      }),
    {
      wrapper: ({ children }: PropsWithChildren) =>
        createElement(QueryClientProvider, { client: queryClient }, children),
    },
  );
}

async function runAndSettle(
  result: ReturnType<typeof renderController>["result"],
  run: () => void,
) {
  act(run);
  await waitFor(() => expect(result.current.activeAction).toBeNull());
}

function testQueryClient() {
  return new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false },
    },
  });
}

function popupFixture() {
  const close = vi.fn();
  const handoff = vi.fn();
  return {
    close,
    handoff,
    value: { close, handoff } satisfies AwsAuthorizationPopup,
  };
}

function deferred<T>() {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

function awsConnection(phase: "ready" | "disconnect_draining" = "ready"): AwsConnection {
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
      error_code: null,
      error_message: null,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:01Z",
    },
    pending_authorization: null,
    retiring_authorization: null,
    revision: 1,
    accepts_placement: phase === "ready",
    can_manage_existing_capacity: phase === "ready",
    available_actions: phase === "ready" ? ["validate", "reconnect", "remove"] : [],
    detail: "AWS connection state",
    customer_action: null,
    next_retry_at: null,
    created_at: "2026-07-20T00:00:00Z",
    updated_at: "2026-07-20T00:00:01Z",
  };
}

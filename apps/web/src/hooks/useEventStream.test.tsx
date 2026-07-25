import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useEventStream } from "@/hooks/useEventStream";
import type { ServerSentEvent } from "@/lib/api/sse";

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useEventStream", () => {
  it("delivers parsed frames and closes when reconnect is disabled", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(streamResponse(['id: 1\nevent: status\ndata: {"ok":true}\n\n'])),
    );

    const events: ServerSentEvent[] = [];
    const { result } = renderHook(() =>
      useEventStream("/api/v1/tasks/t-1/subscribe", {
        reconnect: false,
        onEvent: (event) => events.push(event),
      }),
    );

    await waitFor(() => {
      expect(result.current).toBe("closed");
    });
    expect(events).toEqual([{ id: "1", event: "status", data: '{"ok":true}' }]);
  });

  it("stays idle when disabled", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      useEventStream("/api/v1/events/stream", {
        enabled: false,
        onEvent: () => undefined,
      }),
    );

    expect(result.current).toBe("idle");
    expect(fetchMock).not.toHaveBeenCalled();
  });


  it("backs off repeated connection failures exponentially", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);

    try {
      renderHook(() =>
        useEventStream("/api/v1/events/stream", {
          onEvent: () => undefined,
        }),
      );

      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(999);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1_999);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1);
      expect(fetchMock).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("backs off event streams that repeatedly close before becoming healthy", async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamResponse([]));
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);

    try {
      await act(async () => {
        renderHook(() =>
          useEventStream("/api/v1/events/stream", {
            onEvent: () => undefined,
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(fetchMock).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(999);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1_999);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1);
      expect(fetchMock).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("aborts the previous stream when the workspace URL changes", async () => {
    const fetchMock = vi.fn((_: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { rerender, unmount } = renderHook(
      ({ url }) => useEventStream(url, { onEvent: () => undefined }),
      { initialProps: { url: "/api/v1/events/changes/stream?workspace=one" } },
    );
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const firstSignal = fetchMock.mock.calls[0]?.[1]?.signal;

    rerender({ url: "/api/v1/events/changes/stream?workspace=two" });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(firstSignal?.aborted).toBe(true);

    const secondSignal = fetchMock.mock.calls[1]?.[1]?.signal;
    unmount();
    expect(secondSignal?.aborted).toBe(true);
  });
});

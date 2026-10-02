import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useEventStream, useReconnectingStream } from "@/hooks/useEventStream";
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

describe("useEventStream", () => {
  it("resumes after the last delivered event when the server ends the stream", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(streamResponse(['id: 7\nevent: change\ndata: {"seq":7}\n\n']))
      .mockReturnValue(new Promise(() => undefined));
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);

    try {
      const events: ServerSentEvent[] = [];
      renderHook(() =>
        useEventStream("/v1/workspaces/dev/changes/stream", {
          onEvent: (event) => events.push(event),
        }),
      );
      await vi.waitFor(() => expect(events).toHaveLength(1));
      await vi.advanceTimersByTimeAsync(1_000);

      expect(fetchMock).toHaveBeenCalledTimes(2);
      const headers = fetchMock.mock.calls[1]?.[1]?.headers as Headers;
      expect(headers.get("Last-Event-ID")).toBe("7");
    } finally {
      vi.useRealTimers();
    }
  });

  it("closes a stream whose connection reports nothing more to come", async () => {
    const connect = vi.fn(async ({ onOpen }: { onOpen: () => void }) => {
      onOpen();
      return "done" as const;
    });
    const { result } = renderHook(() => useReconnectingStream("logs", connect));

    await waitFor(() => expect(result.current).toBe("closed"));
    expect(connect).toHaveBeenCalledOnce();
  });

  it("stays idle when disabled", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      useEventStream("/v1/workspaces/dev/changes/stream", {
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
        useEventStream("/v1/workspaces/dev/changes/stream", {
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
          useEventStream("/v1/workspaces/dev/changes/stream", {
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
    const fetchMock = vi.fn(
      (_: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { rerender, unmount } = renderHook(
      ({ url }) => useEventStream(url, { onEvent: () => undefined }),
      { initialProps: { url: "/v1/workspaces/one/changes/stream" } },
    );
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const firstSignal = fetchMock.mock.calls[0]?.[1]?.signal;

    rerender({ url: "/v1/workspaces/two/changes/stream" });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(firstSignal?.aborted).toBe(true);

    const secondSignal = fetchMock.mock.calls[1]?.[1]?.signal;
    unmount();
    expect(secondSignal?.aborted).toBe(true);
  });
});

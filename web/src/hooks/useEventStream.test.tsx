import { renderHook, waitFor } from "@testing-library/react";
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

  it.each([
    ["fail to connect", () => Promise.reject(new Error("offline"))],
    ["close before any event", () => Promise.resolve(streamResponse([]))],
    ["answer 503", () => Promise.resolve(new Response("unavailable", { status: 503 }))],
  ])("waits longer before each retry while connections %s", async (_, answer) => {
    const attempts: number[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        attempts.push(Date.now());
        return answer();
      }),
    );
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);

    renderHook(() =>
      useEventStream("/v1/workspaces/dev/changes/stream", { onEvent: () => undefined }),
    );
    await vi.waitFor(async () => {
      await vi.advanceTimersToNextTimerAsync();
      expect(attempts.length).toBeGreaterThanOrEqual(4);
    });

    const waits = attempts.slice(1, 4).map((time, index) => time - attempts[index]!);
    expect(waits[0]).toBeGreaterThan(0);
    expect(waits[1]).toBeGreaterThan(waits[0]!);
    expect(waits[2]).toBeGreaterThan(waits[1]!);
  });

  it("stops on a client error the server would repeat", async () => {
    const fetchMock = vi.fn(async () => new Response("forbidden", { status: 403 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();

    const { result } = renderHook(() =>
      useEventStream("/v1/workspaces/dev/changes/stream", { onEvent: () => undefined }),
    );
    await vi.waitFor(() => expect(result.current).toBe("error"));
    await vi.advanceTimersByTimeAsync(120_000);

    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("opens nothing while disabled and aborts the previous stream when the URL changes", async () => {
    const fetchMock = vi.fn(
      (_: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender, unmount } = renderHook(
      ({ url, enabled }) => useEventStream(url, { enabled, onEvent: () => undefined }),
      { initialProps: { url: "/v1/workspaces/one/changes/stream", enabled: false } },
    );
    expect(result.current).toBe("idle");
    expect(fetchMock).not.toHaveBeenCalled();

    rerender({ url: "/v1/workspaces/one/changes/stream", enabled: true });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const firstSignal = fetchMock.mock.calls[0]?.[1]?.signal;

    rerender({ url: "/v1/workspaces/two/changes/stream", enabled: true });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(firstSignal?.aborted).toBe(true);

    const secondSignal = fetchMock.mock.calls[1]?.[1]?.signal;
    unmount();
    expect(secondSignal?.aborted).toBe(true);
  });
});

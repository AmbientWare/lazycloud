import { describe, expect, it, vi } from "vitest";

import { streamServerSentEvents, type ServerSentEvent } from "@/lib/api/sse";

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

describe("streamServerSentEvents", () => {
  it("parses frames split across network chunks, skipping comments", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          streamResponse([
            ': connected\n\nid: 1\nevent: status\ndata: {"status":"running"}\n\n',
            "id: 7\neve",
            "nt: log\ndata: par",
            "tial line\n\n",
          ]),
        ),
    );

    const events: ServerSentEvent[] = [];
    const onOpen = vi.fn();
    await streamServerSentEvents("/v1/workspaces/dev/changes/stream", {
      signal: new AbortController().signal,
      onOpen,
      onEvent: (event) => events.push(event),
    });

    expect(onOpen).toHaveBeenCalledOnce();
    expect(events).toEqual([
      { id: "1", event: "status", data: '{"status":"running"}' },
      { id: "7", event: "log", data: "partial line" },
    ]);
  });

  it("sends Last-Event-ID for resume", async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await streamServerSentEvents("/v1/workspaces/dev/changes/stream", {
      signal: new AbortController().signal,
      lastEventId: "42",
      onEvent: () => undefined,
    });

    const headers = fetchMock.mock.calls[0][1].headers as Headers;
    expect(headers.get("Last-Event-ID")).toBe("42");
    expect(headers.get("Accept")).toBe("text/event-stream");
  });

  it("rejects on HTTP errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("workspace not found", { status: 404 })),
    );

    await expect(
      streamServerSentEvents("/v1/workspaces/dev/changes/stream", {
        signal: new AbortController().signal,
        onEvent: () => undefined,
      }),
    ).rejects.toThrow("workspace not found");
  });

  it("rejects successful responses that are not event streams", async () => {
    const onOpen = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<!doctype html><title>LazyCloud</title>", {
          status: 200,
          headers: { "Content-Type": "text/html; charset=utf-8" },
        }),
      ),
    );

    await expect(
      streamServerSentEvents("/v1/workspaces/dev/changes/stream", {
        signal: new AbortController().signal,
        onOpen,
        onEvent: () => undefined,
      }),
    ).rejects.toThrow("Expected text/event-stream response, received text/html");
    expect(onOpen).not.toHaveBeenCalled();
  });
});

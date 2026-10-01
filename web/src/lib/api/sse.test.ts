import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import { workspaceChangeEventSchema } from "@/lib/api/schemas";
import { streamServerSentEvents, type ServerSentEvent } from "@/lib/api/sse";
import { rememberWorkspaces } from "@/lib/api/workspaces";

const WORKSPACE_ID = "0199a000-0000-7000-8000-000000000001";

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
  it("parses id, event, and data fields into frames", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          streamResponse([
            ": connected\n\n",
            'id: 1\nevent: status\ndata: {"status":"running"}\n\n',
            'id: 2\nevent: log\ndata: {"message":"hello"}\n\n',
          ]),
        ),
    );

    const events: ServerSentEvent[] = [];
    const onOpen = vi.fn();
    await streamServerSentEvents("/api/v1/tasks/t-1/subscribe", {
      signal: new AbortController().signal,
      onOpen,
      onEvent: (event) => events.push(event),
    });

    expect(onOpen).toHaveBeenCalledOnce();
    expect(events).toEqual([
      { id: "1", event: "status", data: '{"status":"running"}' },
      { id: "2", event: "log", data: '{"message":"hello"}' },
    ]);
  });

  it("handles frames split across network chunks", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(streamResponse(["id: 7\neve", "nt: log\ndata: par", "tial line\n\n"])),
    );

    const events: ServerSentEvent[] = [];
    await streamServerSentEvents("/api/v1/events/stream", {
      signal: new AbortController().signal,
      onEvent: (event) => events.push(event),
    });

    expect(events).toEqual([{ id: "7", event: "log", data: "partial line" }]);
  });

  it("sends Last-Event-ID for resume", async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await streamServerSentEvents("/api/v1/events/stream", {
      signal: new AbortController().signal,
      lastEventId: "42",
      onEvent: () => undefined,
    });

    const headers = fetchMock.mock.calls[0][1].headers as Headers;
    expect(headers.get("Last-Event-ID")).toBe("42");
    expect(headers.get("Accept")).toBe("text/event-stream");
  });

  it("follows the workspace's change stream as one change event per changed resource", async () => {
    rememberWorkspaces([{ id: WORKSPACE_ID, name: "dev" }]);
    const change = {
      seq: 9,
      workspace_id: WORKSPACE_ID,
      occurred_at: "2026-10-01T12:00:00Z",
      changes: [
        {
          topic: "tasks",
          change: "updated",
          resource_id: "t-1",
          task_id: "t-1",
          root_task_id: "t-0",
        },
        { topic: "containers", change: "created", resource_id: "c-1", container_id: "c-1" },
      ],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        streamResponse([
          `id: 9\nevent: change\ndata: ${JSON.stringify(change)}\n\n`,
          'id: 12\nevent: reset\ndata: {"reason":"behind"}\n\n',
        ]),
      );
    vi.stubGlobal("fetch", fetchMock);

    const events: ServerSentEvent[] = [];
    await streamServerSentEvents(`/api/v1/events/changes/stream?workspace=${WORKSPACE_ID}`, {
      signal: new AbortController().signal,
      lastEventId: "8",
      onEvent: (event) => events.push(event),
    });

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/workspaces/dev/changes/stream");
    expect((fetchMock.mock.calls[0][1].headers as Headers).get("Last-Event-ID")).toBe("8");
    const parsed = events.map((event) => ({
      id: event.id,
      event: event.event,
      change: workspaceChangeEventSchema.parse(JSON.parse(event.data)),
    }));
    expect(parsed.slice(0, 2)).toMatchObject([
      {
        id: "9",
        event: "workspace.change",
        change: { topic: "tasks", task_id: "t-1", root_task_id: "t-0" },
      },
      { id: "9", event: "workspace.change", change: { topic: "containers", container_id: "c-1" } },
    ]);
    // A reset reloads every topic the dashboard shows and resumes after it.
    expect(parsed.slice(2).map((event) => [event.id, event.change.topic])).toEqual([
      ["12", "apps"],
      ["12", "workloads"],
      ["12", "tasks"],
      ["12", "containers"],
      ["12", "storage.secrets"],
    ]);
  });

  it("rejects on HTTP errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("workspace not found", { status: 404 })),
    );

    await expect(
      streamServerSentEvents("/api/v1/events/stream", {
        signal: new AbortController().signal,
        onEvent: () => undefined,
      }),
    ).rejects.toThrow("workspace not found");
  });

  it("preserves typed expired-cursor conflicts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response('{"detail":"realtime cursor is older than retained history"}', {
          status: 409,
          statusText: "Conflict",
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const failure = streamServerSentEvents("/api/v1/events/stream?clamp=false", {
      signal: new AbortController().signal,
      lastEventId: "1-0",
      onEvent: () => undefined,
    });

    await expect(failure).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      message: "realtime cursor is older than retained history",
    } satisfies Partial<ApiError>);
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
      streamServerSentEvents("/api/v1/events/stream", {
        signal: new AbortController().signal,
        onOpen,
        onEvent: () => undefined,
      }),
    ).rejects.toThrow("Expected text/event-stream response, received text/html");
    expect(onOpen).not.toHaveBeenCalled();
  });
});

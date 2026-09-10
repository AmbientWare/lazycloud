import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";

import { LogViewer } from "./index";

it("keeps loaded logs visible when older history fails and retries the missing page", async () => {
  vi.useFakeTimers();
  let failOlderPage = true;
  let sink: ReadableStreamDefaultController<Uint8Array> | undefined;
  const stream = new ReadableStream<Uint8Array>({
    start: (controller) => {
      sink = controller;
    },
  });
  vi.stubGlobal("fetch", async (input: string) => {
    if (input.includes("/stream")) {
      return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
    }
    const older = new URL(input, "https://lazycloud.dev").searchParams.has("cursor");
    if (older && failOlderPage) return new Response("History unavailable", { status: 503 });
    return Response.json({
      data: [
        {
          id: older ? "old" : "new",
          seq_num: older ? 1 : 2,
          timestamp: older ? "2026-09-07T00:00:01Z" : "2026-09-07T00:00:02Z",
          message: older ? "older output" : "recent output",
        },
      ],
      next: older ? "" : "older-page",
    });
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <LogViewer workspaceId="workspace-1" scope={{ taskId: "task-1" }} />
    </QueryClientProvider>,
  );
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(screen.getByText("recent output")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "Load older logs" }));
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(screen.getByText("recent output")).toBeVisible();

  failOlderPage = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry loading older logs" }));
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(screen.getByText("recent output")).toBeVisible();
  expect(screen.getByText("older output")).toBeVisible();
  view.unmount();
  sink?.close();
  client.clear();
});

it("appends live output and bounds the visible history during a large burst", async () => {
  let sink: ReadableStreamDefaultController<Uint8Array> | undefined;
  const stream = new ReadableStream<Uint8Array>({
    start: (controller) => {
      sink = controller;
    },
  });
  vi.stubGlobal("fetch", async (input: string) =>
    input.includes("/stream")
      ? new Response(stream, { headers: { "Content-Type": "text/event-stream" } })
      : Response.json({ data: [], next: "" }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <LogViewer workspaceId="workspace-1" scope={{ taskId: "task-1" }} />
    </QueryClientProvider>,
  );
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
  const encoder = new TextEncoder();
  await act(async () => {
    sink?.enqueue(
      encoder.encode(
        Array.from({ length: 2_500 }, (_, index) => {
          const record = {
            id: String(index),
            seq_num: index,
            timestamp: "2026-09-07T00:00:01Z",
            message: `output-${index}`,
          };
          return `id: ${index}\nevent: log\ndata: ${JSON.stringify(record)}\n\n`;
        }).join(""),
      ),
    );
  });
  expect(screen.getAllByRole("listitem")).toHaveLength(1_000);
  expect(screen.getByText("output-2499")).toBeVisible();
  expect(screen.queryByText("output-1499")).not.toBeInTheDocument();
  await act(async () => {
    sink?.enqueue(
      encoder.encode(
        'id: 2500\nevent: log\ndata: {"id":"2500","seq_num":2500,"timestamp":"2026-09-07T00:00:02Z","message":"still live"}\n\n',
      ),
    );
  });
  expect(screen.getByText("still live")).toBeVisible();
  expect(screen.getAllByRole("listitem")).toHaveLength(1_000);
  view.unmount();
  sink?.close();
  client.clear();
});

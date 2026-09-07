import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";

import { LogViewer } from "./index";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
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

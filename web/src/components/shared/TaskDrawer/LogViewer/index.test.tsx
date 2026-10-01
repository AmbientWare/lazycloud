import { testQueryClient } from "@/test/query-client";
import { act, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";

import { rememberWorkspaces } from "@/lib/api/workspaces";

import { LogViewer } from "./index";

it("appends live output and bounds the visible history during a large burst", async () => {
  vi.useFakeTimers();
  let sink: ReadableStreamDefaultController<Uint8Array> | undefined;
  const stream = new ReadableStream<Uint8Array>({
    start: (controller) => {
      sink = controller;
    },
  });
  // The task's log endpoint: the stored tail, then the followed stream.
  vi.stubGlobal("fetch", async (input: Request) =>
    new URL(input.url).searchParams.get("follow") === "true"
      ? new Response(stream, { headers: { "Content-Type": "application/x-ndjson" } })
      : new Response("", { headers: { "Content-Type": "application/x-ndjson" } }),
  );
  rememberWorkspaces([{ id: "workspace-1", name: "workspace" }]);
  const client = testQueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <LogViewer workspaceId="workspace-1" scope={{ taskId: "task-1" }} />
    </QueryClientProvider>,
  );
  await act(async () => vi.advanceTimersByTimeAsync(50));
  const encoder = new TextEncoder();
  await act(async () => {
    sink?.enqueue(
      encoder.encode(
        Array.from({ length: 2_500 }, (_, index) => {
          const entry = {
            id: index + 1,
            task_id: "task-1",
            attempt: 1,
            stream: "stdout",
            time: "2026-09-07T00:00:01Z",
            data: `output-${index}`,
          };
          return `${JSON.stringify(entry)}\n`;
        }).join(""),
      ),
    );
    await vi.advanceTimersByTimeAsync(50);
  });
  expect(screen.getAllByRole("listitem")).toHaveLength(1_000);
  expect(screen.getByText("output-2499")).toBeVisible();
  expect(screen.queryByText("output-1499")).not.toBeInTheDocument();
  await act(async () => {
    sink?.enqueue(
      encoder.encode(
        '{"id":2501,"task_id":"task-1","attempt":1,"stream":"stdout","time":"2026-09-07T00:00:02Z","data":"still live"}\n',
      ),
    );
    await vi.advanceTimersByTimeAsync(50);
  });
  expect(screen.getByText("still live")).toBeVisible();
  expect(screen.getAllByRole("listitem")).toHaveLength(1_000);
  view.unmount();
  sink?.close();
});

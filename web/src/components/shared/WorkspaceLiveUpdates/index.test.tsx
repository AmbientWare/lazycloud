import { testQueryClient } from "@/test/query-client";
import { act, render } from "@testing-library/react";
import { focusManager, QueryClientProvider, useQuery, type QueryKey } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { WorkspaceLiveUpdatesProvider } from "./index";

const WORKSPACE_ID = "0199a000-0000-7000-8000-000000000001";
const WORKSPACE = "dev";

function changeFrame(sequence: number): string {
  const event = {
    seq: sequence,
    workspace_id: WORKSPACE_ID,
    occurred_at: "2026-07-13T15:30:00Z",
    changes: [
      {
        topic: "tasks",
        change: "updated",
        resource_id: `task-${sequence}`,
        task_id: `task-${sequence}`,
        status: "running",
      },
    ],
  };
  return `id: ${sequence}\nevent: change\ndata: ${JSON.stringify(event)}\n\n`;
}

/** One open server-sent-event response the test writes frames into. */
function openStream() {
  const encoder = new TextEncoder();
  let sink: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      sink = controller;
    },
  });
  return {
    response: new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
    write: (frame: string) => sink?.enqueue(encoder.encode(frame)),
  };
}

/**
 * A live query nothing else would refresh. The long `staleTime` is what keeps
 * TanStack's own refetch-on-focus out of the way, so a refetch after the tab
 * comes back is the provider's recovery and not the default.
 */
function Watcher({ queryKey, onFetch }: { queryKey: QueryKey; onFetch: () => void }) {
  useQuery({
    queryKey,
    queryFn: () => {
      onFetch();
      return Promise.resolve(null);
    },
    staleTime: 600_000,
    meta: workspaceLiveQueryMeta(true),
  });
  return null;
}

async function mountProvider(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  const client = testQueryClient({ defaultOptions: { queries: { retry: false } } });
  const summaryFetches = vi.fn();
  const view = render(
    <QueryClientProvider client={client}>
      <Watcher queryKey={workspaceQueryKeys.apps.summaries(WORKSPACE)} onFetch={summaryFetches} />
      <WorkspaceLiveUpdatesProvider workspace={WORKSPACE}>{null}</WorkspaceLiveUpdatesProvider>
    </QueryClientProvider>,
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(100);
  });
  return { client, summaryFetches, view };
}

afterEach(() => {
  focusManager.setFocused(undefined);
});

describe("workspace live updates", () => {
  it("holds an expensive aggregate to one refetch per cooldown under a burst", async () => {
    vi.useFakeTimers();
    const stream = openStream();
    const { summaryFetches, view } = await mountProvider(
      vi.fn().mockResolvedValue(stream.response),
    );

    const baseline = summaryFetches.mock.calls.length;
    for (let sequence = 1; sequence <= 20; sequence += 1) stream.write(changeFrame(sequence));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(summaryFetches).toHaveBeenCalledTimes(baseline + 1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(14_000);
    });
    stream.write(changeFrame(21));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(summaryFetches).toHaveBeenCalledTimes(baseline + 2);

    view.unmount();
  });

  it("follows the workspace's change stream and reloads what it shows after a reset", async () => {
    vi.useFakeTimers();
    const stream = openStream();
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    const { summaryFetches, view } = await mountProvider(fetchMock);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/v1/workspaces/dev/changes/stream");

    // A resumed stream skips the reconcile on open; the reset is what reloads.
    stream.write(changeFrame(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(16_000);
    });
    const baseline = summaryFetches.mock.calls.length;
    stream.write('id: 2\nevent: reset\ndata: {"reason":"behind"}\n\n');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(summaryFetches).toHaveBeenCalledTimes(baseline + 1);

    view.unmount();
  });

  it("drops the stream while the tab is hidden and recovers the gap on return", async () => {
    vi.useFakeTimers();
    const streams = [openStream(), openStream()];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(streams[0].response)
      .mockResolvedValue(streams[1].response);
    const { summaryFetches, view } = await mountProvider(fetchMock);

    const openSignal = fetchMock.mock.calls[0]?.[1]?.signal as AbortSignal | undefined;
    await act(async () => {
      focusManager.setFocused(false);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(openSignal?.aborted).toBe(true);

    const baseline = summaryFetches.mock.calls.length;
    await act(async () => {
      focusManager.setFocused(true);
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(summaryFetches).toHaveBeenCalledTimes(baseline + 1);

    view.unmount();
  });
});

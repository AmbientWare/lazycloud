import { testQueryClient } from "@/test/query-client";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LogViewer } from "./index";

const encoder = new TextEncoder();

function line(id: number, data: string): string {
  return `${JSON.stringify({ id, task_id: "task-1", attempt: 1, stream: "stdout", time: "2026-09-07T00:00:01Z", data })}\n`;
}

/** A log response the test writes lines into and closes. */
function openBody() {
  let sink: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start: (controller) => {
      sink = controller;
    },
  });
  return {
    response: () => new Response(body, { headers: { "Content-Type": "application/x-ndjson" } }),
    write: (text: string) => sink?.enqueue(encoder.encode(text)),
    close: () => sink?.close(),
  };
}

type Route = (url: URL) => Response | Promise<Response>;

function stubApi(routes: { follow: Route; task?: Route }) {
  const fetchMock = vi.fn(async (input: Request) => {
    const url = new URL(input.url);
    if (url.pathname.endsWith("/logs")) {
      return url.searchParams.get("follow") === "true"
        ? routes.follow(url)
        : new Response("", { headers: { "Content-Type": "application/x-ndjson" } });
    }
    return routes.task?.(url) ?? Response.json({}, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function mount() {
  const client = testQueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LogViewer workspace="dev" source={{ task: "task-1" }} />
    </QueryClientProvider>,
  );
}

const followCalls = (fetchMock: ReturnType<typeof stubApi>) =>
  fetchMock.mock.calls
    .map(([request]) => new URL(request.url))
    .filter((url) => url.searchParams.get("follow") === "true");

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("log viewer", () => {
  it("appends live output and bounds the visible history during a large burst", async () => {
    vi.useFakeTimers();
    const body = openBody();
    stubApi({ follow: () => body.response() });
    const view = mount();
    await act(async () => vi.advanceTimersByTimeAsync(50));
    await act(async () => {
      body.write(
        Array.from({ length: 2_500 }, (_, index) => line(index + 1, `output-${index}`)).join(""),
      );
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.getAllByRole("listitem")).toHaveLength(1_000);
    expect(screen.getByText("output-2499")).toBeVisible();
    expect(screen.queryByText("output-1499")).not.toBeInTheDocument();
    await act(async () => {
      body.write(line(2501, "still live"));
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.getByText("still live")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(1_000);
    view.unmount();
  });

  it("resumes after a server restart and stops once the task has finished", async () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const bodies = [openBody(), openBody()];
    let status = "running";
    const fetchMock = stubApi({
      follow: () => bodies[followCalls(fetchMock).length - 1].response(),
      task: () => Response.json({ id: "task-1", status }),
    });
    mount();
    await act(async () => vi.advanceTimersByTimeAsync(50));

    // A rolling deploy ends the stream cleanly while the task still runs.
    await act(async () => {
      bodies[0].write(line(5, "before the restart"));
      bodies[0].close();
      await vi.advanceTimersByTimeAsync(1_100);
    });
    expect(followCalls(fetchMock).map((url) => url.searchParams.get("after"))).toEqual([null, "5"]);

    // The task finishes and the server ends its stream: nothing more can come.
    status = "succeeded";
    await act(async () => {
      bodies[1].write(line(6, "after the restart"));
      bodies[1].close();
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(followCalls(fetchMock)).toHaveLength(2);
    expect(screen.getByText("after the restart")).toBeVisible();
    expect(screen.getByText("Closed")).toBeVisible();
  });

  it("reconnects a stream that misses its keep-alives", async () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const bodies = [openBody(), openBody()];
    const fetchMock = stubApi({
      follow: () => bodies[followCalls(fetchMock).length - 1].response(),
    });
    mount();
    await act(async () => vi.advanceTimersByTimeAsync(50));

    // A blank keep-alive line resets the read timeout.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
      bodies[0].write("\n");
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(followCalls(fetchMock)).toHaveLength(1);

    await act(async () => vi.advanceTimersByTimeAsync(11_100));
    expect(followCalls(fetchMock)).toHaveLength(2);
  });
});

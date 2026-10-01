import { expect, it, vi } from "vitest";

import { readLogHistory } from "./logs";

it("opens a request's logs at its newest lines, as a task's open", async () => {
  const lines = Array.from({ length: 1_300 }, (_, index) => ({
    id: index + 1,
    stream: "stdout",
    data: `line-${index + 1}`,
    time: "2026-10-01T12:00:00Z",
  }));
  const fetchMock = vi.fn(async (input: Request) => {
    const query = new URL(input.url).searchParams;
    const after = Number(query.get("after"));
    return Response.json({ data: lines.filter((line) => line.id > after).slice(0, 1_000) });
  });
  vi.stubGlobal("fetch", fetchMock);

  const history = await readLogHistory(
    "dev",
    { request: "request-1" },
    new AbortController().signal,
  );

  expect(history.map((line) => line.id)).toEqual(
    Array.from({ length: 200 }, (_, index) => 1_101 + index),
  );
  expect(
    fetchMock.mock.calls.map(([request]) => new URL(request.url).searchParams.get("after")),
  ).toEqual(["0", "1000"]);
});

it("opens a container's own output at its newest lines, though that stream has no tail", async () => {
  const body = Array.from({ length: 250 }, (_, index) =>
    JSON.stringify({
      id: index + 1,
      stream: "stderr",
      data: `line-${index + 1}`,
      time: "2026-10-01T12:00:00Z",
    }),
  ).join("\n");
  const fetchMock = vi.fn(async (_input: Request) => new Response(`${body}\n`));
  vi.stubGlobal("fetch", fetchMock);

  const history = await readLogHistory(
    "dev",
    { output: "container-1" },
    new AbortController().signal,
  );

  const request = new URL(fetchMock.mock.calls[0]![0].url);
  expect(request.pathname).toBe("/v1/workspaces/dev/containers/container-1/output");
  expect(request.searchParams.has("tail")).toBe(false);
  expect(history.map((line) => line.id)).toEqual(
    Array.from({ length: 200 }, (_, index) => 51 + index),
  );
});

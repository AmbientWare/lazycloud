import { expect, it } from "vitest";

import { readNdjson } from "./ndjson";

function body(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

it("delivers lines split across chunks and skips keep-alive blank lines", async () => {
  const seen: unknown[] = [];
  await readNdjson(
    body(['{"id":1,"data":"a', 'b"}\n\n{"id":2,', '"data":"ü"}\n', "\n", '{"id":3,"data":"c"}']),
    (item) => seen.push(item),
  );
  expect(seen).toEqual([
    { id: 1, data: "ab" },
    { id: 2, data: "ü" },
    { id: 3, data: "c" },
  ]);
});

it("splits a multi-byte character across chunks without corrupting it", async () => {
  const bytes = new TextEncoder().encode('{"data":"€"}\n');
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes.slice(0, 10));
      controller.enqueue(bytes.slice(10));
      controller.close();
    },
  });
  const seen: unknown[] = [];
  await readNdjson(stream, (item) => seen.push(item));
  expect(seen).toEqual([{ data: "€" }]);
});

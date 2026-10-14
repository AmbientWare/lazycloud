import { expect, it } from "vitest";

import { readNdjson } from "./ndjson";

it("delivers lines split across chunks, multi-byte characters included, and skips blank keep-alives", async () => {
  const bytes = new TextEncoder().encode('{"id":1,"data":"a"}\n\n{"id":2,"data":"€"}\n\n{"id":3}');
  // Cut inside the first line, inside the euro sign and inside the last line.
  const euro = bytes.indexOf(0xe2);
  const cuts = [0, 10, euro + 1, bytes.length - 3, bytes.length];
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let index = 1; index < cuts.length; index++) {
        controller.enqueue(bytes.slice(cuts[index - 1], cuts[index]));
      }
      controller.close();
    },
  });
  const seen: unknown[] = [];
  await readNdjson(stream, (item) => seen.push(item));
  expect(seen).toEqual([{ id: 1, data: "a" }, { id: 2, data: "€" }, { id: 3 }]);
});

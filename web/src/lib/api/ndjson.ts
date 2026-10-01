/**
 * Read a newline-delimited JSON body, calling `onItem` with each value as its
 * line completes. Blank lines are keep-alives and are skipped. Resolves when
 * the server ends the body; rejects when the connection fails or a line is not
 * JSON.
 */
export async function readNdjson<T>(
  body: ReadableStream<Uint8Array>,
  onItem: (item: T) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (line: string) => {
    if (line.trim() === "") return;
    onItem(JSON.parse(line) as T);
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) consume(line);
    }
    consume(buffer + decoder.decode());
  } finally {
    reader.releaseLock();
  }
}

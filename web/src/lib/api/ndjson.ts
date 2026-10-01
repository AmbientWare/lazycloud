/** Nothing, not even a keep-alive, arrived within the read timeout. */
export class StreamIdleError extends Error {
  constructor(idleMs: number) {
    super(`No data for ${Math.round(idleMs / 1000)} seconds`);
    this.name = "StreamIdleError";
  }
}

/**
 * Read a newline-delimited JSON body, calling `onItem` with each value as its
 * line completes. Blank lines are keep-alives and are skipped. Resolves when
 * the server ends the body; rejects when the connection fails, a line is not
 * JSON, or `idleMs` passes without a byte.
 */
export async function readNdjson<T>(
  body: ReadableStream<Uint8Array>,
  onItem: (item: T) => void,
  { idleMs }: { idleMs?: number } = {},
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (line: string) => {
    if (line.trim() === "") return;
    onItem(JSON.parse(line) as T);
  };
  const read = () => {
    if (idleMs === undefined) return reader.read();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const idle = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new StreamIdleError(idleMs)), idleMs);
    });
    return Promise.race([reader.read(), idle]).finally(() => clearTimeout(timer));
  };
  try {
    for (;;) {
      const { done, value } = await read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) consume(line);
    }
    consume(buffer + decoder.decode());
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }
}

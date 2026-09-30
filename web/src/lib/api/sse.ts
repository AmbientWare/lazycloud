import { getStoredAuthToken } from "@/lib/auth";
import { ApiError } from "@/lib/api/client";

export type ServerSentEvent = {
  id: string;
  event: string;
  data: string;
};

/**
 * Consume a `text/event-stream` response over fetch so the bearer token can be
 * sent as a header (EventSource cannot set Authorization). Resolves when the
 * server closes the stream; rejects on network or HTTP errors.
 */
export async function streamServerSentEvents(
  url: string,
  {
    signal,
    lastEventId,
    onOpen,
    onEvent,
  }: {
    signal: AbortSignal;
    lastEventId?: string;
    onOpen?: () => void;
    onEvent: (event: ServerSentEvent) => void;
  },
): Promise<void> {
  const headers = new Headers({ Accept: "text/event-stream" });
  const token = getStoredAuthToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (lastEventId) headers.set("Last-Event-ID", lastEventId);

  const response = await fetch(url, { headers, signal, credentials: "include" });
  if (!response.ok || !response.body) {
    const body = await response.text().catch(() => "");
    throw new ApiError(response.status, response.statusText, body, {
      requestId: response.headers.get("X-Request-ID") ?? "",
      retryAfter: response.headers.get("Retry-After"),
    });
  }
  const mediaType = response.headers.get("Content-Type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (mediaType !== "text/event-stream") {
    await response.body.cancel().catch(() => undefined);
    throw new Error(
      `Expected text/event-stream response, received ${mediaType || "no content type"}`,
    );
  }
  onOpen?.();

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let current: ServerSentEvent = { id: "", event: "message", data: "" };

  const dispatch = () => {
    if (current.data) onEvent({ ...current, data: current.data.replace(/\n$/, "") });
    current = { id: current.id, event: "message", data: "" };
  };

  const consumeLine = (line: string) => {
    if (line === "") {
      dispatch();
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") current.id = value;
    else if (field === "event") current.event = value;
    else if (field === "data") current.data += `${value}\n`;
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split(/\r\n|\r|\n/);
    buffer = lines.pop() ?? "";
    for (const line of lines) consumeLine(line);
  }
  if (buffer) consumeLine(buffer);
  dispatch();
}

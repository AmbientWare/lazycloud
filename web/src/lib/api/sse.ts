import { ApiError, api, ok, type Schemas } from "@/lib/api/client";
import { openLogStream, viewLogRecord } from "@/lib/api/logs";
import { readNdjson } from "@/lib/api/ndjson";
import { workspaceName } from "@/lib/api/workspaces";

export type ServerSentEvent = {
  id: string;
  event: string;
  data: string;
};

/**
 * Consume a `text/event-stream` response over fetch, authenticated by the
 * session cookie. Resolves when the server closes the stream; rejects on
 * network or HTTP errors. A log stream is read from the API's newline-delimited
 * log endpoints and delivered as the same events.
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
  const target = new URL(url, "http://dashboard.invalid");
  if (target.pathname === "/api/v1/logs/stream") {
    await streamLogs(target.searchParams, { signal, lastEventId, onOpen, onEvent });
    return;
  }
  const headers = new Headers({ Accept: "text/event-stream" });
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

/**
 * Follow the log lines of the scope the log viewer names: the newest `limit`
 * lines, then each new one, resuming after the last delivered line.
 */
async function streamLogs(
  params: URLSearchParams,
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
  const workspace = workspaceName(params.get("workspace") ?? "");
  const after = lastEventId ? Number(lastEventId) : 0;
  const body = await openLogStream(
    workspace,
    async (app, name) => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/deployments", {
          params: { path: { workspace }, query: { app, name } },
        }),
      );
      return page.deployments[0]?.id;
    },
    {
      taskId: params.get("task_id") ?? undefined,
      containerId: params.get("container_id") ?? undefined,
      stubId: params.get("stub_id") ?? undefined,
    },
    {
      after: after || undefined,
      tail: Number(params.get("limit") ?? "200"),
      follow: params.get("follow") === "true",
      signal,
    },
  );
  onOpen?.();
  await readNdjson<Schemas["LogEntry"]>(body, (entry) =>
    onEvent({ id: String(entry.id), event: "message", data: JSON.stringify(viewLogRecord(entry)) }),
  );
  // A followed task or container stream ends with the task or container; no
  // line can follow, so stay open rather than reconnect to an ended scope.
  await new Promise<void>((resolve) => {
    if (signal.aborted) resolve();
    else signal.addEventListener("abort", () => resolve(), { once: true });
  });
}

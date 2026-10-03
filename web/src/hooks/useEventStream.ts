import { useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api/client";
import { streamServerSentEvents, type ServerSentEvent } from "@/lib/api/sse";

const INITIAL_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;
const RECONNECT_JITTER_RATIO = 0.25;
const STABLE_CONNECTION_RESET_MS = 30_000;

export type EventStreamStatus =
  "idle" | "connecting" | "open" | "reconnecting" | "closed" | "error";

/**
 * One connection of a resumable stream. It calls `onOpen` once the server
 * answers, keeps its resume point in `cursor`, which lives as long as the
 * subscription, and resolves "done" when nothing more can arrive or
 * "reconnect" when the server ended a stream that has more to send.
 */
export type StreamConnect = (session: {
  signal: AbortSignal;
  cursor: { value?: string };
  onOpen: () => void;
}) => Promise<"done" | "reconnect">;

/**
 * Keep a stream connected while `key` is set: reconnect with jittered
 * exponential backoff after a close or a failure, reset the backoff once a
 * connection stays up, and stop on "done" or a client error the server will
 * answer the same way again. A new key starts a new subscription; `connect`
 * is read on each attempt, so it may change between renders.
 */
export function useReconnectingStream(
  key: string | null,
  connect: StreamConnect,
  { onOpen, onReconnect }: { onOpen?: () => void; onReconnect?: () => void } = {},
): EventStreamStatus {
  const [status, setStatus] = useState<EventStreamStatus>("connecting");
  const connectRef = useRef(connect);
  const onOpenRef = useRef(onOpen);
  const onReconnectRef = useRef(onReconnect);

  useEffect(() => {
    connectRef.current = connect;
    onOpenRef.current = onOpen;
    onReconnectRef.current = onReconnect;
  });

  useEffect(() => {
    if (key === null) return;
    const controller = new AbortController();
    const cursor: { value?: string } = {};
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let stableTimer: ReturnType<typeof setTimeout> | undefined;
    let hasConnected = false;
    let attempt = 0;

    const clearStableTimer = () => {
      if (stableTimer !== undefined) clearTimeout(stableTimer);
      stableTimer = undefined;
    };

    const scheduleReconnect = () => {
      const delay = Math.min(INITIAL_RECONNECT_DELAY_MS * 2 ** attempt, MAX_RECONNECT_DELAY_MS);
      const jitter = delay * RECONNECT_JITTER_RATIO * (Math.random() * 2 - 1);
      attempt += 1;
      setStatus("reconnecting");
      reconnectTimer = setTimeout(run, Math.round(delay + jitter));
    };

    function run() {
      setStatus(hasConnected || attempt > 0 ? "reconnecting" : "connecting");
      connectRef
        .current({
          signal: controller.signal,
          cursor,
          onOpen: () => {
            const recovered = hasConnected || attempt > 0;
            hasConnected = true;
            clearStableTimer();
            stableTimer = setTimeout(() => {
              attempt = 0;
              stableTimer = undefined;
            }, STABLE_CONNECTION_RESET_MS);
            setStatus("open");
            onOpenRef.current?.();
            if (recovered) onReconnectRef.current?.();
          },
        })
        .then((outcome) => {
          clearStableTimer();
          if (controller.signal.aborted) return;
          if (outcome === "done") setStatus("closed");
          else scheduleReconnect();
        })
        .catch((error: unknown) => {
          clearStableTimer();
          if (controller.signal.aborted) return;
          if (
            error instanceof ApiError &&
            error.status >= 400 &&
            error.status < 500 &&
            error.status !== 408 &&
            error.status !== 429
          ) {
            setStatus("error");
            return;
          }
          scheduleReconnect();
        });
    }

    run();
    return () => {
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      clearStableTimer();
      controller.abort();
    };
  }, [key]);

  return key === null ? "idle" : status;
}

/**
 * Follow a server-sent-event endpoint, resuming with `Last-Event-ID` after
 * each reconnect. The latest render's `onEvent` receives every parsed frame.
 */
export function useEventStream(
  url: string | null,
  {
    enabled = true,
    onEvent,
    onOpen,
    onReconnect,
  }: {
    enabled?: boolean;
    onEvent: (event: ServerSentEvent) => void;
    onOpen?: () => void;
    onReconnect?: () => void;
  },
): EventStreamStatus {
  const key = url && enabled ? url : null;
  const onEventRef = useRef(onEvent);
  useEffect(() => {
    onEventRef.current = onEvent;
  });
  return useReconnectingStream(
    key,
    async ({ signal, cursor, onOpen: opened }) => {
      await streamServerSentEvents(key ?? "", {
        signal,
        lastEventId: cursor.value,
        onOpen: opened,
        onEvent: (event) => {
          if (event.id) cursor.value = event.id;
          onEventRef.current(event);
        },
      });
      return "reconnect";
    },
    { onOpen, onReconnect },
  );
}

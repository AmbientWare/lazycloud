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
 * Follow a server-sent-event endpoint with automatic reconnect and
 * `Last-Event-ID` resume. `onEvent` receives every parsed frame.
 *
 * When `reconnect` is false the hook stops after the server closes the stream
 * (used for streams that end at a terminal state, e.g. task event streams).
 */
export function useEventStream(
  url: string | null,
  {
    enabled = true,
    reconnect = true,
    onEvent,
    onOpen,
    onReconnect,
  }: {
    enabled?: boolean;
    reconnect?: boolean;
    onEvent: (event: ServerSentEvent) => void;
    onOpen?: () => void;
    onReconnect?: () => void;
  },
): EventStreamStatus {
  const [status, setStatus] = useState<EventStreamStatus>("connecting");
  const onEventRef = useRef(onEvent);
  const onOpenRef = useRef(onOpen);
  const onReconnectRef = useRef(onReconnect);
  const active = !!url && enabled;

  useEffect(() => {
    onEventRef.current = onEvent;
    onOpenRef.current = onOpen;
    onReconnectRef.current = onReconnect;
  });

  useEffect(() => {
    if (!url || !enabled) return;
    const streamUrl = url;

    const controller = new AbortController();
    let lastEventId: string | undefined;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let stableConnectionTimer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let hasConnected = false;
    let reconnectAttempt = 0;

    function clearStableConnectionTimer() {
      if (stableConnectionTimer !== undefined) {
        clearTimeout(stableConnectionTimer);
        stableConnectionTimer = undefined;
      }
    }

    function scheduleReconnect() {
      const exponentialDelay = Math.min(
        INITIAL_RECONNECT_DELAY_MS * 2 ** reconnectAttempt,
        MAX_RECONNECT_DELAY_MS,
      );
      const jitter = exponentialDelay * RECONNECT_JITTER_RATIO * (Math.random() * 2 - 1);
      reconnectAttempt += 1;
      setStatus("reconnecting");
      reconnectTimer = setTimeout(connect, Math.round(exponentialDelay + jitter));
    }

    function connect() {
      setStatus(hasConnected || reconnectAttempt > 0 ? "reconnecting" : "connecting");
      streamServerSentEvents(streamUrl, {
        signal: controller.signal,
        lastEventId,
        onOpen: () => {
          const recovered = hasConnected || reconnectAttempt > 0;
          hasConnected = true;
          clearStableConnectionTimer();
          stableConnectionTimer = setTimeout(() => {
            reconnectAttempt = 0;
            stableConnectionTimer = undefined;
          }, STABLE_CONNECTION_RESET_MS);
          setStatus("open");
          onOpenRef.current?.();
          if (recovered) onReconnectRef.current?.();
        },
        onEvent: (event) => {
          reconnectAttempt = 0;
          if (event.id) lastEventId = event.id;
          onEventRef.current(event);
        },
      })
        .then(() => {
          clearStableConnectionTimer();
          if (stopped) return;
          if (reconnect) {
            scheduleReconnect();
          } else {
            setStatus("closed");
          }
        })
        .catch((error: unknown) => {
          clearStableConnectionTimer();
          if (stopped || controller.signal.aborted) return;
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
          if (reconnect) {
            scheduleReconnect();
          } else {
            setStatus("error");
          }
        });
    }

    connect();

    return () => {
      stopped = true;
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      clearStableConnectionTimer();
      controller.abort();
    };
  }, [enabled, reconnect, url]);

  return active ? status : "idle";
}

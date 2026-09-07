import { useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal as XTerm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

import {
  encodeJsonFrame,
  encodeShellFrame,
  ShellFrameDecoder,
  ShellFrameType,
} from "@/lib/shell-protocol";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

type ShellCredentials = { username: string; password: string };

type ConnectionState = "connecting" | "authenticating" | "open" | "closed" | "error";

/**
 * xterm.js terminal wired to the gateway shell WebSocket. The connection opens,
 * waits for the proxy "OK" preface, authenticates with the per-session
 * credentials, then streams framed PTY bytes both ways. No fake streaming: the
 * terminal renders exactly what the container shell emits.
 */
export function Terminal({
  socketUrl,
  credentials,
  className,
  onReconnect,
}: {
  socketUrl: string;
  credentials: ShellCredentials;
  className?: string;
  onReconnect: () => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<ConnectionState>("connecting");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    const host = containerRef.current;
    if (!host) return;

    const styles = getComputedStyle(host);
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const term = new XTerm({
      fontSize: 12,
      fontFamily: styles.getPropertyValue("--font-mono").trim(),
      cursorBlink: !motion.matches,
      convertEol: true,
      theme: { background: styles.backgroundColor, foreground: styles.color },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    const updateMotion = () => {
      term.options.cursorBlink = !motion.matches;
    };
    motion.addEventListener("change", updateMotion);
    fit.fit();

    const decoder = new ShellFrameDecoder();
    const socket = new WebSocket(socketUrl);
    socket.binaryType = "arraybuffer";
    let authenticated = false;
    let disposed = false;

    const sendResize = () => {
      if (socket.readyState !== WebSocket.OPEN || !authenticated) return;
      socket.send(
        encodeJsonFrame(ShellFrameType.Resize, {
          cols: term.cols,
          rows: term.rows,
        }),
      );
    };

    const authenticate = () => {
      setState("authenticating");
      socket.send(
        encodeJsonFrame(ShellFrameType.Auth, {
          username: credentials.username,
          password: credentials.password,
          term: "xterm-256color",
          cols: term.cols,
          rows: term.rows,
        }),
      );
    };

    const handleServerFrames = (bytes: Uint8Array) => {
      for (const frame of decoder.feed(bytes)) {
        if (frame.type === ShellFrameType.Ready) {
          authenticated = true;
          setState("open");
          term.focus();
          sendResize();
        } else if (frame.type === ShellFrameType.Data) {
          term.write(frame.payload);
        } else if (frame.type === ShellFrameType.Error) {
          setErrorMessage(new TextDecoder().decode(frame.payload) || "shell error");
          setState("error");
          socket.close();
        } else if (frame.type === ShellFrameType.Exit) {
          const code = new TextDecoder().decode(frame.payload) || "0";
          term.write(`\r\n\x1b[90m[session ended, exit ${code}]\x1b[0m\r\n`);
          setState("closed");
          socket.close();
        }
      }
    };

    socket.onmessage = (event) => {
      // The proxy sends a text "OK" preface once the backend socket is dialed;
      // authenticate only after it arrives. Everything else is binary frames.
      if (typeof event.data === "string") {
        if (!authenticated && event.data.includes("OK")) authenticate();
        return;
      }
      handleServerFrames(new Uint8Array(event.data as ArrayBuffer));
    };

    socket.onerror = () => {
      if (disposed) return;
      setState("error");
      setErrorMessage((current) => current ?? "connection failed");
    };

    socket.onclose = (event) => {
      if (disposed) return;
      // The tunnel reports backend dial failures in the close frame reason
      // (code 1011 from the shells router); surface it instead of a silent
      // disconnect.
      if (event.code !== 1000 && event.reason) {
        setErrorMessage((current) => current ?? event.reason);
        setState("error");
        return;
      }
      setState((current) => (current === "error" || current === "closed" ? current : "closed"));
    };

    const inputDisposable = term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN && authenticated) {
        socket.send(encodeShellFrame(ShellFrameType.Data, new TextEncoder().encode(data)));
      }
    });

    const resizeObserver = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        // xterm throws if the host has zero size mid-transition; ignore.
      }
    });
    resizeObserver.observe(host);
    const disposeResize = term.onResize(() => sendResize());

    return () => {
      disposed = true;
      motion.removeEventListener("change", updateMotion);
      resizeObserver.disconnect();
      inputDisposable.dispose();
      disposeResize.dispose();
      socket.close();
      term.dispose();
    };
  }, [socketUrl, credentials.username, credentials.password]);

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className="flex items-center gap-2 px-1 pb-1.5 text-[11px] text-muted-foreground">
        <span
          className={cn(
            "size-1.5 rounded-full",
            state === "open"
              ? "bg-positive"
              : state === "error"
                ? "bg-destructive"
                : state === "closed"
                  ? "bg-muted-foreground/50"
                  : "bg-warning",
          )}
        />
        <span>{connectionLabel(state)}</span>
        {errorMessage ? <span className="text-destructive">{errorMessage}</span> : null}
        {state === "closed" || state === "error" ? (
          <Button variant="outline" size="sm" className="ml-auto" onClick={onReconnect}>
            Reconnect
          </Button>
        ) : null}
      </div>
      <div
        ref={containerRef}
        className="min-h-0 flex-1 overflow-hidden rounded-md bg-background text-foreground p-2"
      />
    </div>
  );
}

function connectionLabel(state: ConnectionState): string {
  switch (state) {
    case "connecting":
      return "connecting";
    case "authenticating":
      return "authenticating";
    case "open":
      return "connected";
    case "closed":
      return "disconnected";
    case "error":
      return "error";
  }
}

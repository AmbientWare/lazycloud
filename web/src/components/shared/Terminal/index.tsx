import { useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal as XTerm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

import { cn } from "@/lib/utils";

type ConnectionState = "connecting" | "open" | "closed" | "error";

/** A text message the shell sends; binary messages are terminal bytes. */
type ShellMessage = { type: "exit"; code: number } | { type: "error"; message: string };

/**
 * xterm.js terminal on a container's shell WebSocket. Binary messages carry
 * terminal bytes both ways; resizes go up and the exit or a start failure come
 * down as JSON text. The terminal renders exactly what the shell emits.
 */
export function Terminal({
  workspace,
  containerId,
  className,
}: {
  workspace: string;
  containerId: string;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<ConnectionState>("connecting");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    const host = containerRef.current;
    if (!host) return;

    const style = getComputedStyle(host);
    const term = new XTerm({
      fontSize: 12,
      fontFamily: style.fontFamily,
      cursorBlink: true,
      convertEol: true,
      theme: {
        background: style.backgroundColor,
        foreground: style.color,
        cursor: style.getPropertyValue("--brand").trim(),
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();

    const socket = new WebSocket(
      containerShellUrl(workspace, containerId, { cols: term.cols, rows: term.rows }),
    );
    socket.binaryType = "arraybuffer";
    const encoder = new TextEncoder();
    let disposed = false;

    const sendResize = () => {
      if (socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    };

    socket.onopen = () => {
      setState("open");
      term.focus();
      // The terminal may have been fitted again while the socket opened.
      sendResize();
    };

    socket.onmessage = (event) => {
      if (typeof event.data !== "string") {
        term.write(new Uint8Array(event.data as ArrayBuffer));
        return;
      }
      let message: ShellMessage;
      try {
        message = JSON.parse(event.data) as ShellMessage;
      } catch {
        return;
      }
      if (message.type === "exit") {
        term.write(`\r\n\x1b[90m[session ended, exit ${message.code}]\x1b[0m\r\n`);
        setState("closed");
      } else if (message.type === "error") {
        setErrorMessage(message.message || "shell error");
        setState("error");
      }
    };

    socket.onerror = () => {
      if (disposed) return;
      setState("error");
      setErrorMessage((current) => current ?? "connection failed");
    };

    socket.onclose = (event) => {
      if (disposed) return;
      // A refused or failed shell names its cause in the close reason.
      if (event.code !== 1000 && event.reason) {
        setErrorMessage((current) => current ?? event.reason);
        setState("error");
        return;
      }
      setState((current) => (current === "error" || current === "closed" ? current : "closed"));
    };

    const inputDisposable = term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(encoder.encode(data));
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
      resizeObserver.disconnect();
      inputDisposable.dispose();
      disposeResize.dispose();
      socket.close();
      term.dispose();
    };
  }, [workspace, containerId]);

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className="flex items-center gap-2 px-1 pb-1.5 text-[11px] text-muted-foreground">
        <span
          className={cn(
            "size-1.5 rounded-full",
            state === "open"
              ? "pulse-live bg-positive"
              : state === "error"
                ? "bg-destructive"
                : state === "closed"
                  ? "bg-muted-foreground/50"
                  : "bg-warning",
          )}
        />
        <span>{connectionLabel(state)}</span>
        {errorMessage ? <span className="text-destructive">{errorMessage}</span> : null}
      </div>
      <div
        ref={containerRef}
        className="min-h-0 flex-1 overflow-hidden rounded-md bg-background p-2 font-mono text-foreground"
      />
    </div>
  );
}

/**
 * The shell's WebSocket URL. The session cookie and the page's Origin
 * authenticate the upgrade, so the URL carries no credential.
 */
function containerShellUrl(
  workspace: string,
  containerId: string,
  size: { cols: number; rows: number },
): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const path = `/v1/workspaces/${encodeURIComponent(workspace)}/containers/${encodeURIComponent(containerId)}/shell`;
  const query = new URLSearchParams({
    cols: String(size.cols),
    rows: String(size.rows),
    term: "xterm-256color",
  });
  return `${protocol}//${window.location.host}${path}?${query.toString()}`;
}

function connectionLabel(state: ConnectionState): string {
  switch (state) {
    case "connecting":
      return "connecting";
    case "open":
      return "connected";
    case "closed":
      return "disconnected";
    case "error":
      return "error";
  }
}

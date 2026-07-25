import { postJson, withWorkspace } from "@/lib/api/client";
import { shellSessionSchema, type ShellSession } from "@/lib/api/schemas";

/**
 * Start (or reuse) the shell server inside a running container and mint
 * per-session credentials. Requires a write-capable token.
 */
export function createContainerShell(
  workspaceId: string,
  containerId: string,
): Promise<ShellSession> {
  return postJson(
    withWorkspace("/api/v1/shells/existing-container", workspaceId),
    shellSessionSchema,
    {
      container_id: containerId,
    },
  );
}

/**
 * WebSocket tunnel to the in-container shell server. Browsers cannot send an
 * Authorization header on WebSocket upgrade, so the single-use shell ticket
 * is the only credential placed in the URL.
 */
export function shellWebSocketUrl(
  stubId: string,
  containerId: string,
  websocketTicket: string,
): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const path = `/api/v1/shells/id/${encodeURIComponent(stubId)}/${encodeURIComponent(containerId)}/ws`;
  return `${protocol}//${window.location.host}${path}?ticket=${encodeURIComponent(websocketTicket)}`;
}

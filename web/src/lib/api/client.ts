import createClient from "openapi-fetch";

import type { components, paths } from "@/lib/api/generated/openapi";
import { clearStoredAuthToken } from "@/lib/auth";

/** Every schema of the public API, generated from contracts/openapi.yaml. */
export type Schemas = components["schemas"];

export class ApiError extends Error {
  readonly status: number;
  readonly statusText: string;
  readonly body: string;
  readonly requestId: string;
  readonly retryAt: number | null;

  constructor(
    status: number,
    statusText: string,
    body: string,
    { requestId = "", retryAfter = null }: { requestId?: string; retryAfter?: string | null } = {},
  ) {
    super(responseErrorMessage(status, statusText, body));
    this.name = "ApiError";
    this.status = status;
    this.statusText = statusText;
    this.body = body;
    this.requestId = requestId;
    this.retryAt = retryAfterAt(retryAfter);
  }
}

export class ApiProtocolError extends Error {
  constructor(message = "The API returned an unexpected response", options?: ErrorOptions) {
    super(message, options);
    this.name = "ApiProtocolError";
  }
}

/** The message of the API's typed error body, else the HTTP status. */
export function responseErrorMessage(status: number, statusText: string, body: string): string {
  const fallback = `${status} ${statusText}`;
  if (!body) return fallback;
  try {
    const payload: unknown = JSON.parse(body);
    if (typeof payload === "object" && payload !== null && "message" in payload) {
      const { message } = payload as { message: unknown };
      if (typeof message === "string" && message.trim()) return message.trim();
    }
    return fallback;
  } catch {
    if (/^\s*(?:<!doctype\s+html|<html\b)/i.test(body)) return fallback;
    return body.trim() || fallback;
  }
}

function retryAfterAt(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return Date.now() + seconds * 1_000;
  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp) ? null : Math.max(timestamp, Date.now());
}

export function clearAuthToken(): void {
  clearStoredAuthToken();
}

/**
 * The public API, typed by path and method. Requests go to this origin and the
 * HttpOnly session cookie authenticates them; the browser's own Origin header
 * is what lets a cookie-authenticated change through.
 */
export const api = createClient<paths>({
  baseUrl: typeof window === "undefined" ? "http://localhost" : window.location.origin,
  credentials: "same-origin",
  // Resolved per request rather than captured at import, so the page's fetch,
  // including a test's stand-in, is the one that runs.
  fetch: (request) => globalThis.fetch(request),
});

type Outcome = { data?: unknown; error?: unknown; response: Response };

/** The success body of an operation, or void for one that answers 204. */
type Body<R extends Outcome> = [Exclude<R["data"], undefined>] extends [never]
  ? void
  : Exclude<R["data"], undefined>;

/**
 * The response body of a successful typed call. A failure throws the same
 * ApiError the dashboard reports everywhere; a 401 ends the browser's session.
 */
export async function ok<R extends Outcome>(pending: Promise<R>): Promise<Body<R>> {
  const { data, error, response } = await pending;
  if (!response.ok) {
    if (response.status === 401) clearStoredAuthToken();
    throw new ApiError(
      response.status,
      response.statusText,
      typeof error === "string" ? error : JSON.stringify(error ?? ""),
      {
        requestId: response.headers.get("X-Request-ID") ?? "",
        retryAfter: response.headers.get("Retry-After"),
      },
    );
  }
  return data as Body<R>;
}

import createClient from "openapi-fetch";
import { z } from "zod";

import type { components, paths } from "@/lib/api/generated/openapi";
import { errorResponseSchema } from "@/lib/api/schemas";
import { clearStoredAuthToken, setStoredAuthToken } from "@/lib/auth";

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

export function responseErrorMessage(status: number, statusText: string, body: string): string {
  if (!body) return `${status} ${statusText}`;
  try {
    const payload: unknown = JSON.parse(body);
    if (!isRecord(payload)) return `${status} ${statusText}`;
    const standardized = errorResponseSchema.safeParse(payload);
    if (standardized.success && standardized.data.detail.trim()) {
      return standardized.data.detail.trim();
    }
    const detail = payload.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (Array.isArray(detail)) {
      const messages = detail.flatMap((entry) => validationMessage(entry));
      if (messages.length > 0) return messages.join("; ");
    }
    const message = payload.message;
    if (typeof message === "string" && message.trim()) return message.trim();
    return `${status} ${statusText}`;
  } catch {
    if (/^\s*(?:<!doctype\s+html|<html\b)/i.test(body)) {
      return `${status} ${statusText}`;
    }
    return body.trim() || `${status} ${statusText}`;
  }
}

function validationMessage(value: unknown): string[] {
  if (!isRecord(value) || typeof value.msg !== "string") return [];
  const location = Array.isArray(value.loc)
    ? value.loc.filter(
        (part): part is string | number => typeof part === "string" || typeof part === "number",
      )
    : [];
  const field = location.at(-1);
  return [field === undefined ? value.msg : `${String(field)}: ${value.msg}`];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Name the workspace a request acts on, and let membership authorize it. A request
 * that names none resolves the default rather than guessing among the account's others.
 */
export function withWorkspace(path: string, workspaceId: string): string {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}workspace=${encodeURIComponent(workspaceId)}`;
}

/**
 * A correlation id for one request.
 *
 * `crypto.randomUUID` is restricted to secure contexts, so it is undefined when
 * the dashboard is served over plain HTTP from anything other than localhost —
 * reaching for it directly makes every request throw before it is sent, which
 * surfaces as the control plane being unreachable.
 */
function newRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`;
}

async function apiResponse(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const clientRequestId = headers.get("X-Request-ID") || newRequestId();
  headers.set("X-Request-ID", clientRequestId);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "include",
  });

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    // 401 means the token itself is invalid; 403 is an authorization decision
    // (for example a non-admin token reaching an admin-only route) and must
    // not log the user out.
    if (response.status === 401) clearStoredAuthToken();
    throw new ApiError(response.status, response.statusText, body, {
      requestId: response.headers.get("X-Request-ID") || clientRequestId,
      retryAfter: response.headers.get("Retry-After"),
    });
  }

  return response;
}

export async function apiRequest<T>(
  path: string,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
  init: RequestInit = {},
): Promise<T> {
  const response = await apiResponse(path, init);
  if (response.status === 204) return schema.parse(null);
  const text = await response.text();
  let json: unknown = null;
  if (text) {
    try {
      json = JSON.parse(text);
    } catch (error) {
      throw new ApiProtocolError(
        /^\s*(?:<!doctype\s+html|<html\b)/i.test(text)
          ? "The API route returned the frontend shell instead of JSON"
          : "The API returned invalid JSON",
        { cause: error },
      );
    }
  }
  return schema.parse(json);
}

/**
 * Fetch raw bytes through the same auth and error handling as `apiRequest`.
 *
 * Artifact content is served by the control plane rather than linked directly
 * at the object store, so it needs a bearer token like any other API call.
 */
export async function apiBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const response = await apiResponse(path, { signal });
  return response.blob();
}

function retryAfterAt(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return Date.now() + seconds * 1_000;
  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp) ? null : Math.max(timestamp, Date.now());
}

export function setAuthToken(token: string): void {
  setStoredAuthToken(token.trim());
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

export async function postJson<T>(
  path: string,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
  body: unknown = {},
): Promise<T> {
  return apiRequest(path, schema, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

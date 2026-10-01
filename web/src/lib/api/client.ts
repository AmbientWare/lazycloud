import createClient from "openapi-fetch";

import type { components, paths } from "./generated/openapi";

/** Every schema of the public API, generated from contracts/openapi.yaml. */
export type Schemas = components["schemas"];
export type ErrorCode = Schemas["ErrorCode"];

/**
 * A refusal or failure the API answered with. `code` is the typed error code
 * when the body was the API's error object; a proxy or gateway page leaves it
 * null and the message falls back to the HTTP status.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: ErrorCode | null;

  constructor(status: number, code: ErrorCode | null, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/**
 * The public API, typed by path and method. Requests go to this origin: the
 * browser session cookie authenticates them, and the browser's own Origin
 * header is what lets a cookie-authenticated mutation through.
 */
export const api = createClient<paths>({
  baseUrl: typeof window === "undefined" ? "" : window.location.origin,
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

/** The response body of a successful call; anything else throws an ApiError. */
export async function ok<R extends Outcome>(pending: Promise<R>): Promise<Body<R>> {
  const { data, error, response } = await pending;
  if (!response.ok) throw apiError(response.status, response.statusText, error);
  return data as Body<R>;
}

export function apiError(status: number, statusText: string, body: unknown): ApiError {
  if (isErrorBody(body)) return new ApiError(status, body.code, body.message);
  return new ApiError(status, null, `${status} ${statusText}`.trim());
}

function isErrorBody(body: unknown): body is Schemas["Error"] {
  return (
    typeof body === "object" &&
    body !== null &&
    typeof (body as { code?: unknown }).code === "string" &&
    typeof (body as { message?: unknown }).message === "string"
  );
}

export function isApiError(error: unknown, status?: number): error is ApiError {
  return error instanceof ApiError && (status === undefined || error.status === status);
}

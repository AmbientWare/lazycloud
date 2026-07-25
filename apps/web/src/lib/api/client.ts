import { z } from "zod";

import {
  errorResponseSchema,
  workspaceListSchema,
  type Workspace,
} from "@/lib/api/schemas";
import { clearStoredAuthToken, getStoredAuthToken, setStoredAuthToken } from "@/lib/auth";

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

export function responseErrorMessage(
  status: number,
  statusText: string,
  body: string,
): string {
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
    ? value.loc.filter((part): part is string | number =>
        typeof part === "string" || typeof part === "number",
      )
    : [];
  const field = location.at(-1);
  return [field === undefined ? value.msg : `${String(field)}: ${value.msg}`];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Append the admin cross-workspace `workspace` query param. Without it the API
 * scopes every request to the bearer token's own workspace.
 */
export function withWorkspace(path: string, workspaceId: string): string {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}workspace=${encodeURIComponent(workspaceId)}`;
}

export async function apiRequest<T>(
  path: string,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  const clientRequestId = headers.get("X-Request-ID") || crypto.randomUUID();
  headers.set("X-Request-ID", clientRequestId);
  const token = getStoredAuthToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
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

export async function listWorkspaces(
  options: { includeDeleting?: boolean } = {},
): Promise<Workspace[]> {
  const path = options.includeDeleting
    ? "/api/v1/workspaces?include_deleting=true"
    : "/api/v1/workspaces";
  const response = await apiRequest(path, workspaceListSchema);
  return response.workspaces;
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

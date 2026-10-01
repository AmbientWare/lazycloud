import type { z } from "zod";

import { ApiError } from "@/lib/api/client";

/*
 * Reads and commands of the reference dashboard that no merged packet of the
 * public API serves yet: billing, compute, observability, pods, devboxes,
 * sandboxes, shells and endpoint URLs. Their pages stay so each packet can wire
 * its own; until then every call rejects with not_found, without a request, and
 * the panel shows that the feature is unavailable. A packet that serves one
 * replaces its caller with a typed `api` call. Delete this file once nothing
 * imports it.
 */

/** The refusal, naming the reference operation for whoever wires it. */
class UnservedError extends ApiError {
  readonly operation: string;

  constructor(path: string) {
    super(404, "not_found", "Not available yet");
    this.operation = path.split("?", 1)[0];
  }
}

function unserved(path: string): ApiError {
  return new UnservedError(path);
}

export function withWorkspace(path: string, workspace: string): string {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}workspace=${encodeURIComponent(workspace)}`;
}

/** Stands for a JSON read or command; `schema` types the value it will return. */
export async function apiRequest<T>(
  path: string,
  schema?: z.ZodType<T, z.ZodTypeDef, unknown>,
  init?: RequestInit,
): Promise<T> {
  void schema;
  void init;
  throw unserved(path);
}

export async function postJson<T>(
  path: string,
  schema?: z.ZodType<T, z.ZodTypeDef, unknown>,
  body?: unknown,
): Promise<T> {
  void schema;
  void body;
  throw unserved(path);
}

export async function apiBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  void signal;
  throw unserved(path);
}

import type { Page, Request, Route } from "@playwright/test";
import { z } from "zod";

const API_GLOB = "**/api/v1/**";

export type StrictApiMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type StrictApiQuery = Readonly<Record<string, string | readonly string[]>>;

export type StrictApiRequest<RequestBody> = {
  body: RequestBody;
  request: Request;
  url: URL;
};

export type StrictApiJsonResponse<ResponseBody> = {
  kind: "json";
  status: number;
  body: ResponseBody;
};

export type StrictApiNoContentResponse = {
  kind: "no-content";
  status: 204;
};

export type StrictApiSseResponse = {
  kind: "sse";
  status: number;
  body: string;
};

type JsonRoute<RequestBody, ResponseBody> = {
  method: StrictApiMethod;
  path: string;
  query: StrictApiQuery;
  requestSchema: z.ZodType<RequestBody, z.ZodTypeDef, unknown>;
  responseSchema: z.ZodType<ResponseBody, z.ZodTypeDef, unknown>;
  handler: (
    request: StrictApiRequest<RequestBody>,
  ) => StrictApiJsonResponse<ResponseBody> | Promise<StrictApiJsonResponse<ResponseBody>>;
};

type NoContentRoute<RequestBody> = {
  method: StrictApiMethod;
  path: string;
  query: StrictApiQuery;
  requestSchema: z.ZodType<RequestBody, z.ZodTypeDef, unknown>;
  handler: (
    request: StrictApiRequest<RequestBody>,
  ) => StrictApiNoContentResponse | Promise<StrictApiNoContentResponse>;
};

type SseRoute = {
  method: "GET";
  path: string;
  query: StrictApiQuery;
  handler: (
    request: StrictApiRequest<null>,
  ) => StrictApiSseResponse | Promise<StrictApiSseResponse>;
};

type RegisteredRoute = {
  method: StrictApiMethod;
  path: string;
  query: StrictApiQuery;
  handle: (request: Request, url: URL) => Promise<StrictApiResponse>;
};

type StrictApiResponse =
  StrictApiJsonResponse<unknown> | StrictApiNoContentResponse | StrictApiSseResponse;

export type StrictApiFailureReporter = (failure: Error) => void;

export function jsonResponse<ResponseBody>(
  body: ResponseBody,
  status = 200,
): StrictApiJsonResponse<ResponseBody> {
  return { kind: "json", status, body };
}

export function noContentResponse(): StrictApiNoContentResponse {
  return { kind: "no-content", status: 204 };
}

export function sseResponse(body: string, status = 200): StrictApiSseResponse {
  return { kind: "sse", status, body };
}

export class StrictApiFixture {
  readonly #page: Page;
  readonly #reportFailure: StrictApiFailureReporter;
  readonly #routes = new Map<string, RegisteredRoute>();
  readonly #failures: Error[] = [];

  private constructor(page: Page, reportFailure: StrictApiFailureReporter) {
    this.#page = page;
    this.#reportFailure = reportFailure;
  }

  static async install(
    page: Page,
    reportFailure: StrictApiFailureReporter = (failure) => {
      throw failure;
    },
  ): Promise<StrictApiFixture> {
    const fixture = new StrictApiFixture(page, reportFailure);
    await page.route(API_GLOB, fixture.#handleRoute);
    return fixture;
  }

  get routeCount(): number {
    return this.#routes.size;
  }

  json<RequestBody, ResponseBody>(definition: JsonRoute<RequestBody, ResponseBody>): void {
    this.#registerJson(definition, false);
  }

  replaceJson<RequestBody, ResponseBody>(definition: JsonRoute<RequestBody, ResponseBody>): void {
    this.#registerJson(definition, true);
  }

  noContent<RequestBody>(definition: NoContentRoute<RequestBody>): void {
    this.#register(
      definition,
      async (request, url) => {
        const body = parseRequestBody(request, definition.requestSchema);
        return definition.handler({ body, request, url });
      },
      false,
    );
  }

  replaceNoContent<RequestBody>(definition: NoContentRoute<RequestBody>): void {
    this.#register(
      definition,
      async (request, url) => {
        const body = parseRequestBody(request, definition.requestSchema);
        return definition.handler({ body, request, url });
      },
      true,
    );
  }

  sse(definition: SseRoute): void {
    this.#register(
      definition,
      async (request, url) => {
        const body = parseRequestBody(request, z.null());
        return definition.handler({ body, request, url });
      },
      false,
    );
  }

  replaceSse(definition: SseRoute): void {
    this.#register(
      definition,
      async (request, url) => {
        const body = parseRequestBody(request, z.null());
        return definition.handler({ body, request, url });
      },
      true,
    );
  }

  failures(): readonly Error[] {
    return this.#failures;
  }

  assertNoFailures(): void {
    const failure = this.#failures[0];
    if (failure) throw failure;
  }

  async dispose(): Promise<void> {
    await this.#page.unroute(API_GLOB, this.#handleRoute);
  }

  #registerJson<RequestBody, ResponseBody>(
    definition: JsonRoute<RequestBody, ResponseBody>,
    replace: boolean,
  ): void {
    this.#register(
      definition,
      async (request, url) => {
        const body = parseRequestBody(request, definition.requestSchema);
        const response = await definition.handler({ body, request, url });
        return {
          ...response,
          body: parseResponseBody(response.body, definition.responseSchema),
        };
      },
      replace,
    );
  }

  #register(
    definition: {
      method: StrictApiMethod;
      path: string;
      query: StrictApiQuery;
    },
    handle: RegisteredRoute["handle"],
    replace: boolean,
  ): void {
    const method = normalizeMethod(definition.method);
    const path = normalizePath(definition.path);
    const query = normalizeQueryDefinition(definition.query);
    const key = routeKey(method, path, query);
    const exists = this.#routes.has(key);
    if (replace ? !exists : exists) {
      throw new Error(
        replace
          ? `Cannot replace unregistered strict API route ${key}`
          : `Duplicate strict API route ${key}; use an explicit replacement`,
      );
    }
    this.#routes.set(key, { method, path, query, handle });
  }

  #handleRoute = async (route: Route): Promise<void> => {
    const request = route.request();
    const method = request.method().trim().toUpperCase();
    const url = new URL(request.url());
    const path = normalizePath(url.pathname);
    const query = queryFromUrl(url);
    const key = routeKey(method, path, query);
    const registered = this.#routes.get(key);

    try {
      if (!registered) throw this.#unmatchedRequest(method, path, query);
      const response = await registered.handle(request, url);
      switch (response.kind) {
        case "json":
          await route.fulfill({
            status: response.status,
            contentType: "application/json",
            body: JSON.stringify(response.body),
          });
          return;
        case "no-content":
          await route.fulfill({ status: response.status, body: "" });
          return;
        case "sse":
          await route.fulfill({
            status: response.status,
            contentType: "text/event-stream",
            body: response.body,
          });
          return;
      }
    } catch (cause) {
      const failure = strictApiFailure(method, path, query, cause);
      this.#failures.push(failure);
      await route.abort("failed");
      this.#reportFailure(failure);
    }
  };

  #unmatchedRequest(method: string, path: string, query: StrictApiQuery): Error {
    const requested = routeKey(method, path, query);
    const sameResource = [...this.#routes.keys()].filter((candidate) =>
      candidate.endsWith(` ${path}?${queryKey(query)}`),
    );
    const detail =
      sameResource.length > 0
        ? ` Registered method(s): ${sameResource.map((candidate) => candidate.split(" ")[0]).join(", ")}.`
        : "";
    return new Error(`Unmatched strict API request ${requested}.${detail}`);
  }
}

function parseRequestBody<RequestBody>(
  request: Request,
  schema: z.ZodType<RequestBody, z.ZodTypeDef, unknown>,
): RequestBody {
  const rawBody = request.postData();
  if (rawBody === null || rawBody === "") return schema.parse(null);
  const contentType = request.headers()["content-type"];
  if (!contentType?.toLowerCase().includes("application/json")) {
    throw new Error("Request body must use application/json");
  }
  let payload: unknown;
  try {
    payload = JSON.parse(rawBody);
  } catch (cause) {
    throw new Error("Request body is not valid JSON", { cause });
  }
  return schema.parse(payload);
}

function parseResponseBody<ResponseBody>(
  body: ResponseBody,
  schema: z.ZodType<ResponseBody, z.ZodTypeDef, unknown>,
): ResponseBody {
  return schema.parse(body);
}

function normalizeMethod(method: string): StrictApiMethod {
  const normalized = method.trim().toUpperCase();
  switch (normalized) {
    case "GET":
    case "POST":
    case "PUT":
    case "PATCH":
    case "DELETE":
      return normalized;
    default:
      throw new Error(`Unsupported strict API method ${normalized || "<empty>"}`);
  }
}

function normalizePath(path: string): string {
  const trimmed = path.trim();
  if (!trimmed.startsWith("/api/v1/")) {
    throw new Error(`Strict API path must start with /api/v1/: ${trimmed}`);
  }
  if (trimmed.includes("?")) {
    throw new Error(`Strict API query must be registered separately: ${trimmed}`);
  }
  return trimmed.length > 1 ? trimmed.replace(/\/+$/, "") : trimmed;
}

function normalizeQueryDefinition(query: StrictApiQuery): StrictApiQuery {
  return Object.fromEntries(
    Object.entries(query)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, value]) => [
        key,
        (Array.isArray(value) ? [...value] : [value]).map(String).sort(),
      ]),
  );
}

function queryFromUrl(url: URL): StrictApiQuery {
  const query: Record<string, string[]> = {};
  for (const [key, value] of url.searchParams) {
    query[key] = [...(query[key] ?? []), value];
  }
  return normalizeQueryDefinition(query);
}

function routeKey(method: string, path: string, query: StrictApiQuery): string {
  return `${method} ${path}?${queryKey(query)}`;
}

function queryKey(query: StrictApiQuery): string {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    const values = Array.isArray(value) ? value : [value];
    for (const item of values) parameters.append(key, item);
  }
  return parameters.toString();
}

function strictApiFailure(
  method: string,
  path: string,
  query: StrictApiQuery,
  cause: unknown,
): Error {
  const detail = cause instanceof Error ? cause.message : String(cause);
  return new Error(`Strict API ${routeKey(method, path, query)} failed: ${detail}`, {
    cause,
  });
}

import { afterEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { ApiError, apiRequest, responseErrorMessage } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("responseErrorMessage", () => {
  it("extracts FastAPI detail without rendering JSON", () => {
    const error = new ApiError(403, "Forbidden", '{"detail":"admin scope required"}');
    expect(error.message).toBe("admin scope required");
    expect(error.body).toBe('{"detail":"admin scope required"}');
  });

  it("formats validation errors by field", () => {
    expect(
      responseErrorMessage(
        422,
        "Unprocessable Entity",
        JSON.stringify({
          detail: [
            { loc: ["body", "name"], msg: "Field required", type: "missing" },
            { loc: ["query", "limit"], msg: "Must be positive", type: "value_error" },
          ],
        }),
      ),
    ).toBe("name: Field required; limit: Must be positive");
  });

  it("falls back to the HTTP status for unstructured JSON", () => {
    expect(responseErrorMessage(500, "Internal Server Error", '{"error":true}')).toBe(
      "500 Internal Server Error",
    );
  });

  it("does not render an HTML error document", () => {
    expect(responseErrorMessage(404, "Not Found", "<!DOCTYPE html><html>shell</html>")).toBe(
      "404 Not Found",
    );
  });

  it("reports a successful HTML shell response as a routing error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<!DOCTYPE html><html><body>frontend shell</body></html>", {
          status: 200,
          headers: { "Content-Type": "text/html" },
        }),
      ),
    );

    await expect(apiRequest("/api/v1/pools", z.object({}))).rejects.toThrow(
      "The API route returned the frontend shell instead of JSON",
    );
  });

  it("retains request correlation and rate-limit timing on API failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response('{"detail":"Too many requests"}', {
          status: 429,
          headers: { "Retry-After": "2", "X-Request-ID": "req-server-42" },
        }),
      ),
    );

    const before = Date.now();
    let error: ApiError | undefined;
    try {
      await apiRequest("/api/v1/tasks/task-1", z.object({}));
    } catch (caught) {
      if (caught instanceof ApiError) error = caught;
    }

    expect(error).toMatchObject({ status: 429, requestId: "req-server-42" });
    expect(error?.retryAt).toBeGreaterThanOrEqual(before + 2_000);
  });

  it("sends a client request ID and preserves it when the server does not echo one", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"detail":"Unavailable"}', { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);

    let error: ApiError | undefined;
    try {
      await apiRequest("/api/v1/tasks/task-1", z.object({}));
    } catch (caught) {
      if (caught instanceof ApiError) error = caught;
    }

    const headers = fetchMock.mock.calls[0][1].headers as Headers;
    expect(headers.get("X-Request-ID")).toBeTruthy();
    expect(error?.requestId).toBe(headers.get("X-Request-ID"));
  });
});

import { describe, expect, it, vi } from "vitest";

import { ApiError, api, ok, responseErrorMessage } from "./client";

describe("API errors", () => {
  it("reports the API's typed error message", () => {
    const body = JSON.stringify({ code: "forbidden", message: "Only administrators can do that" });
    const error = new ApiError(403, "Forbidden", body);
    expect(error.message).toBe("Only administrators can do that");
    expect(error.body).toBe(body);
  });

  it("falls back to the HTTP status for an unstructured or HTML body", () => {
    expect(responseErrorMessage(500, "Internal Server Error", '{"error":true}')).toBe(
      "500 Internal Server Error",
    );
    expect(responseErrorMessage(404, "Not Found", "<!DOCTYPE html><html>shell</html>")).toBe(
      "404 Not Found",
    );
  });

  it("keeps the request id and rate-limit timing of a failed call", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          Response.json(
            { code: "rate_limited", message: "Too many requests" },
            { status: 429, headers: { "Retry-After": "2", "X-Request-ID": "req-server-42" } },
          ),
        ),
    );

    const before = Date.now();
    const failure = await ok(api.GET("/v1/me")).catch((caught: unknown) => caught);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ status: 429, requestId: "req-server-42" });
    expect((failure as ApiError).message).toBe("Too many requests");
    expect((failure as ApiError).retryAt).toBeGreaterThanOrEqual(before + 2_000);
  });
});

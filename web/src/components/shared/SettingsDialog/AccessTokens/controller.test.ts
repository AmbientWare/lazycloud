import { testQueryClient } from "@/test/query-client";
import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider, type InfiniteData } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import type { TokenListResponse } from "@/lib/api/schemas";
import { tokensQueryOptions } from "@/lib/queries/tokens";

import { useAccessTokensController } from "./controller";

describe("access tokens controller", () => {
  it("mints once and keeps the issued secret out of the query cache", async () => {
    const existing = token({ id: "existing", name: "existing" });
    const created = token({ id: "created", name: "ci-deploy" });
    const createResponse = deferred<Response>();
    let createRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if ((input as Request).method === "POST") {
        createRequests += 1;
        return createResponse.promise;
      }
      return jsonResponse({ tokens: [existing] });
    });
    const queryClient = testQueryClient();
    const { result } = renderHook(() => useAccessTokensController(false), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(ids(result.current.tokens)).toEqual(["existing"]));

    act(() => result.current.beginCreate());
    act(() => {
      result.current.create({ name: "ci-deploy", expiresInSeconds: null });
      result.current.create({ name: "ci-deploy", expiresInSeconds: null });
    });

    expect(createRequests).toBe(1);
    expect(result.current.createMode).toBe("creating");

    createResponse.resolve(jsonResponse({ token: "lc_9zzone-time-value", record: created }, 201));
    await waitFor(() => expect(result.current.createMode).toBe("issued"));

    expect(result.current.issued).toEqual({
      secret: "lc_9zzone-time-value",
      name: "ci-deploy",
      prefix: "lc_9zz",
    });
    expect(ids(cachedTokens(queryClient))).toEqual(["created", "existing"]);
    expect(serializedQueryState(queryClient)).not.toContain("lc_9zzone-time-value");

    act(() => result.current.dismissIssued());
    expect(result.current.issued).toBeNull();
    expect(result.current.createMode).toBe("closed");
  });

  it("drops the row on delete and keeps the failure retryable", async () => {
    const target = token({ id: "target", name: "target" });
    const sibling = token({ id: "sibling", name: "sibling" });
    let revokeRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if ((input as Request).method === "DELETE") {
        revokeRequests += 1;
        return revokeRequests === 1
          ? jsonResponse({ code: "unavailable", message: "revoke unavailable" }, 503)
          : new Response(null, { status: 204 });
      }
      return jsonResponse({ tokens: [target, sibling] });
    });
    const queryClient = testQueryClient();
    const { result } = renderHook(() => useAccessTokensController(false), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(result.current.tokens).toHaveLength(2));

    act(() => result.current.beginAction(result.current.tokens[0]));
    expect(result.current.actionMode).toBe("confirming");
    act(() => {
      result.current.confirmAction();
      result.current.confirmAction();
    });
    await waitFor(() => expect(result.current.actionMode).toBe("error"));
    expect(revokeRequests).toBe(1);
    expect(result.current.actionError?.message).toBe("revoke unavailable");
    expect(ids(cachedTokens(queryClient))).toEqual(["target", "sibling"]);

    act(() => result.current.confirmAction());
    await waitFor(() => expect(result.current.actionMode).toBe("idle"));
    expect(ids(cachedTokens(queryClient))).toEqual(["sibling"]);
  });
});

function wrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function ids(tokens: readonly { id: string }[]): string[] {
  return tokens.map((item) => item.id);
}

function cachedTokens(queryClient: QueryClient): { id: string }[] {
  const cache = queryClient.getQueryData<InfiniteData<TokenListResponse, string>>(
    tokensQueryOptions(false).queryKey,
  );
  return (cache?.pages ?? []).flatMap((page) => page.data);
}

function serializedQueryState(queryClient: QueryClient): string {
  return JSON.stringify(
    queryClient
      .getQueryCache()
      .getAll()
      .map((query) => query.state.data),
  );
}

/** A token as GET /v1/tokens returns it. */
function token(overrides: Partial<Schemas["Token"]> = {}): Schemas["Token"] {
  return {
    id: "token-1",
    name: "dashboard",
    device: false,
    status: "active",
    created_at: "2026-07-21T12:00:00Z",
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

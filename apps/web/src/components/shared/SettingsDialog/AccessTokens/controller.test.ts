import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthToken, TokenListResponse } from "@/lib/api/schemas";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

import { useAccessTokensController } from "./controller";

afterEach(() => vi.restoreAllMocks());

describe("access tokens controller", () => {
  it("mints once and keeps the issued secret out of the query cache", async () => {
    const existing = token({ id: "existing", name: "existing" });
    const created = token({ id: "created", name: "ci-deploy", prefix: "lc_9zz" });
    const createResponse = deferred<Response>();
    let createRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method === "POST") {
        createRequests += 1;
        return createResponse.promise;
      }
      return jsonResponse({ tokens: [existing] });
    });
    const queryClient = testQueryClient();
    const { result } = renderHook(() => useAccessTokensController(), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(result.current.tokens).toEqual([existing]));

    act(() => result.current.beginCreate());
    act(() => {
      result.current.create({ name: "ci-deploy", expiresInSeconds: null });
      result.current.create({ name: "ci-deploy", expiresInSeconds: null });
    });

    expect(createRequests).toBe(1);
    expect(result.current.createMode).toBe("creating");

    createResponse.resolve(jsonResponse({ token: "one-time-value", record: created }, 201));
    await waitFor(() => expect(result.current.createMode).toBe("issued"));

    expect(result.current.issued).toEqual({
      secret: "one-time-value",
      name: "ci-deploy",
      prefix: "lc_9zz",
    });
    expect(tokenCache(queryClient)).toEqual({ tokens: [existing, created] });
    expect(serializedQueryState(queryClient)).not.toContain("one-time-value");

    act(() => result.current.dismissIssued());
    expect(result.current.issued).toBeNull();
    expect(result.current.createMode).toBe("closed");
  });

  it("applies the exact server record on revoke and drops the row on delete", async () => {
    const target = token({ id: "target", name: "target" });
    const sibling = token({ id: "sibling", name: "sibling" });
    const revoked = token({
      id: "target",
      name: "target",
      status: "revoked",
      revoked_at: "2026-07-21T12:30:00Z",
    });
    let deleteRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.endsWith("/revoke")) return jsonResponse(revoked);
      if (init?.method === "DELETE") {
        deleteRequests += 1;
        return deleteRequests === 1
          ? jsonResponse({ detail: "delete unavailable" }, 503)
          : new Response(null, { status: 204 });
      }
      return jsonResponse({ tokens: [target, sibling] });
    });
    const queryClient = testQueryClient();
    const { result } = renderHook(() => useAccessTokensController(), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(result.current.tokens).toHaveLength(2));

    act(() => result.current.beginAction("revoke", target));
    expect(result.current.actionMode).toBe("confirming");
    act(() => result.current.confirmAction());
    await waitFor(() => expect(result.current.actionMode).toBe("idle"));
    expect(tokenCache(queryClient)).toEqual({ tokens: [revoked, sibling] });

    act(() => result.current.beginAction("delete", revoked));
    act(() => {
      result.current.confirmAction();
      result.current.confirmAction();
    });
    await waitFor(() => expect(result.current.actionMode).toBe("error"));
    expect(deleteRequests).toBe(1);
    expect(result.current.actionError?.message).toBe("delete unavailable");
    expect(tokenCache(queryClient)).toEqual({ tokens: [revoked, sibling] });

    act(() => result.current.confirmAction());
    await waitFor(() => expect(result.current.actionMode).toBe("idle"));
    expect(tokenCache(queryClient)).toEqual({ tokens: [sibling] });
  });
});

function wrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function tokenCache(queryClient: QueryClient): TokenListResponse | undefined {
  return queryClient.getQueryData<TokenListResponse>(accountQueryKeys.tokens());
}

function serializedQueryState(queryClient: QueryClient): string {
  return JSON.stringify(
    queryClient
      .getQueryCache()
      .getAll()
      .map((query) => query.state.data),
  );
}

function token(overrides: Partial<AuthToken> = {}): AuthToken {
  return {
    id: "token-1",
    name: "dashboard",
    prefix: "lc_1234",
    kind: "user",
    user_id: "user-1",
    workspace_id: "",
    status: "active",
    scopes: ["*"],
    reusable: true,
    disabled_by_admin: false,
    created_at: "2026-07-21T12:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
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

import { testQueryClient } from "@/test/query-client";
import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider, type InfiniteData } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AuthToken, TokenListResponse } from "@/lib/api/schemas";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

import { useAccessTokensController } from "./controller";

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
      return jsonResponse({ data: [existing], next: "" });
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
    expect(cachedTokens(queryClient)).toEqual([created, existing]);
    expect(serializedQueryState(queryClient)).not.toContain("one-time-value");

    act(() => result.current.dismissIssued());
    expect(result.current.issued).toBeNull();
    expect(result.current.createMode).toBe("closed");
  });

  it("drops the row on delete and keeps the failure retryable", async () => {
    const target = token({ id: "target", name: "target" });
    const sibling = token({ id: "sibling", name: "sibling" });
    let revokeRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if (String(input).endsWith("/revoke")) {
        revokeRequests += 1;
        return revokeRequests === 1
          ? jsonResponse({ detail: "revoke unavailable" }, 503)
          : jsonResponse(token({ id: "target", name: "target", status: "revoked" }));
      }
      return jsonResponse({ data: [target, sibling], next: "" });
    });
    const queryClient = testQueryClient();
    const { result } = renderHook(() => useAccessTokensController(), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(result.current.tokens).toHaveLength(2));

    act(() => result.current.beginAction(target));
    expect(result.current.actionMode).toBe("confirming");
    act(() => {
      result.current.confirmAction();
      result.current.confirmAction();
    });
    await waitFor(() => expect(result.current.actionMode).toBe("error"));
    expect(revokeRequests).toBe(1);
    expect(result.current.actionError?.message).toBe("revoke unavailable");
    expect(cachedTokens(queryClient)).toEqual([target, sibling]);

    act(() => result.current.confirmAction());
    await waitFor(() => expect(result.current.actionMode).toBe("idle"));
    expect(cachedTokens(queryClient)).toEqual([sibling]);
  });
});

function wrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function cachedTokens(queryClient: QueryClient): AuthToken[] {
  const cache = queryClient.getQueryData<InfiniteData<TokenListResponse, string>>(
    accountQueryKeys.tokens(),
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

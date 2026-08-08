import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthToken, TokenKind, TokenListResponse } from "@/lib/api/schemas";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { isManagedToken, useWorkspaceTokensController } from "./controller";

afterEach(() => vi.restoreAllMocks());

describe("workspace tokens controller", () => {
  it("keeps the one-time secret route-local and inserts only the exact public record", async () => {
    const existing = token({ id: "existing", name: "existing" });
    const accepted = token({ id: "created", name: "deploy" });
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
    const { result } = renderController(queryClient);
    await waitFor(() => expect(result.current.tokens).toEqual([existing]));

    act(() => result.current.beginCreate());
    expect(result.current.createMode).toBe("drafting");
    act(() => {
      const input = {
        name: "deploy",
        scopes: ["read"] as const,
        expiresInSeconds: null,
      };
      result.current.create({ ...input, scopes: [...input.scopes] });
      result.current.create({ ...input, scopes: [...input.scopes] });
    });

    expect(result.current.createMode).toBe("creating");
    expect(createRequests).toBe(1);
    act(() => result.current.cancelCreate());
    act(() => result.current.beginDelete(existing));
    expect(result.current.createMode).toBe("creating");
    expect(result.current.actionMode).toBe("idle");

    createResponse.resolve(jsonResponse({ token: "one-time-value", record: accepted }, 201));
    await waitFor(() => expect(result.current.createMode).toBe("issued"));

    expect(result.current.issuedSecret).toBe("one-time-value");
    expect(tokenCache(queryClient, "workspace-1")).toEqual({
      tokens: [existing, accepted],
    });
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
    expect(serializedQueryState(queryClient)).not.toContain("one-time-value");

    act(() => result.current.acknowledgeIssued());
    expect(result.current.issuedSecret).toBeNull();
    expect(result.current.createMode).toBe("closed");
  });

  it("retries toggle and delete failures while applying exact server records", async () => {
    const target = token({ id: "target", name: "target" });
    const sibling = token({ id: "sibling", name: "sibling" });
    const accepted = token({
      id: "target",
      name: "server-renamed",
      status: "revoked",
      revoked_at: "2026-07-21T12:30:00Z",
    });
    let toggleRequests = 0;
    let deleteRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.includes("/toggle")) {
        toggleRequests += 1;
        return toggleRequests === 1
          ? jsonResponse({ detail: "toggle unavailable" }, 503)
          : jsonResponse(accepted);
      }
      if (init?.method === "DELETE") {
        deleteRequests += 1;
        return deleteRequests === 1
          ? jsonResponse({ detail: "delete unavailable" }, 503)
          : new Response(null, { status: 204 });
      }
      return jsonResponse({ tokens: [target, sibling] });
    });
    const queryClient = testQueryClient();
    const { result } = renderController(queryClient);
    await waitFor(() => expect(result.current.tokens).toHaveLength(2));

    act(() => {
      result.current.toggle(target);
      result.current.toggle(sibling);
    });
    await waitFor(() => expect(result.current.actionMode).toBe("error"));
    expect(result.current.actionKind).toBe("toggle");
    expect(result.current.actionTokenId).toBe("target");
    expect(result.current.actionError?.message).toBe("toggle unavailable");
    expect(toggleRequests).toBe(1);
    expect(tokenCache(queryClient, "workspace-1")).toEqual({
      tokens: [target, sibling],
    });

    act(() => result.current.toggle(target));
    await waitFor(() => expect(result.current.actionMode).toBe("idle"));
    expect(toggleRequests).toBe(2);
    expect(tokenCache(queryClient, "workspace-1")).toEqual({
      tokens: [accepted, sibling],
    });

    act(() => result.current.beginDelete(accepted));
    expect(result.current.actionMode).toBe("confirming");
    act(() => {
      result.current.deleteConfirmed(accepted);
      result.current.deleteConfirmed(accepted);
    });
    await waitFor(() => expect(result.current.actionMode).toBe("error"));
    expect(result.current.actionKind).toBe("delete");
    expect(result.current.actionError?.message).toBe("delete unavailable");
    expect(deleteRequests).toBe(1);
    expect(tokenCache(queryClient, "workspace-1")).toEqual({
      tokens: [accepted, sibling],
    });

    act(() => result.current.deleteConfirmed(accepted));
    await waitFor(() => expect(result.current.actionMode).toBe("idle"));
    expect(deleteRequests).toBe(2);
    expect(tokenCache(queryClient, "workspace-1")).toEqual({ tokens: [sibling] });
  });

  it("treats every non-workspace credential as managed and rejects its commands", async () => {
    const managedKinds: TokenKind[] = [
      "admin",
      "workspace-primary",
      "workspace-restricted",
      "worker",
      "worker-private",
      "machine",
    ];
    const managed = managedKinds.map((kind, index) => token({ id: `managed-${index}`, kind }));
    const foreign = token({ id: "foreign", workspace_id: "workspace-2" });
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ tokens: managed }));
    const { result } = renderController(testQueryClient());
    await waitFor(() => expect(result.current.tokens).toHaveLength(managed.length));

    expect(managed.every(isManagedToken)).toBe(true);
    act(() => {
      for (const credential of managed) {
        result.current.toggle(credential);
        result.current.beginDelete(credential);
        result.current.deleteConfirmed(credential);
      }
      result.current.toggle(foreign);
      result.current.beginDelete(foreign);
      result.current.deleteConfirmed(foreign);
    });

    expect(fetch).toHaveBeenCalledOnce();
    expect(result.current.actionMode).toBe("idle");
  });

  it("drops secret and command state immediately when the workspace changes", async () => {
    const first = token({ id: "first" });
    const created = token({ id: "created" });
    const second = token({
      id: "second",
      workspace_id: "workspace-2",
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (init?.method === "POST") {
        return jsonResponse({ token: "workspace-one-value", record: created }, 201);
      }
      return path.includes("workspace=workspace-2")
        ? jsonResponse({ tokens: [second] })
        : jsonResponse({ tokens: [first] });
    });
    const queryClient = testQueryClient();
    const { result, rerender } = renderHook(
      ({ workspaceId }: { workspaceId: string }) => useWorkspaceTokensController(workspaceId),
      {
        initialProps: { workspaceId: "workspace-1" },
        wrapper: controllerWrapper(queryClient),
      },
    );
    await waitFor(() => expect(result.current.tokens).toEqual([first]));
    act(() => result.current.beginCreate());
    act(() =>
      result.current.create({
        name: "created",
        scopes: ["read"],
        expiresInSeconds: null,
      }),
    );
    await waitFor(() => expect(result.current.issuedSecret).toBe("workspace-one-value"));

    rerender({ workspaceId: "workspace-2" });

    expect(result.current.issuedSecret).toBeNull();
    expect(result.current.createMode).toBe("closed");
    expect(result.current.actionMode).toBe("idle");
    await waitFor(() => expect(result.current.tokens).toEqual([second]));
    expect(tokenCache(queryClient, "workspace-1")).toEqual({
      tokens: [first, created],
    });
    expect(serializedQueryState(queryClient)).not.toContain("workspace-one-value");
  });
});

function renderController(queryClient: QueryClient) {
  return renderHook(() => useWorkspaceTokensController("workspace-1"), {
    wrapper: controllerWrapper(queryClient),
  });
}

function controllerWrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false },
    },
  });
}

function tokenCache(queryClient: QueryClient, workspaceId: string): TokenListResponse | undefined {
  return queryClient.getQueryData<TokenListResponse>(
    workspaceQueryKeys.settings.tokens(workspaceId),
  );
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
    kind: "workspace",
    user_id: "",
    workspace_id: "workspace-1",
    status: "active",
    scopes: ["read", "write"],
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

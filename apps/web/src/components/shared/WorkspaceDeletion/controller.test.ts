import { testQueryClient } from "@/test/query-client";
import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CurrentSession, Workspace } from "@/lib/api/schemas";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { useWorkspaceDeletionController } from "./controller";

describe("workspace deletion controller", () => {
  it("removes only the accepted workspace and preserves global and sibling state", async () => {
    const target = workspace("workspace-2", "research");
    const sibling = workspace("workspace-1", "default");
    const queryClient = testQueryClient();
    const replacePath = vi.fn();
    const rememberWorkspaceName = vi.fn();
    const deleteCommand = vi.fn().mockResolvedValue(null);
    queryClient.setQueryData(currentSessionQueryOptions().queryKey, {
      user: sessionUser(),
      workspaces: [sibling, target],
    });
    queryClient.setQueryData(workspaceQueryKeys.apps.root(target.id), ["target"]);
    queryClient.setQueryData(workspaceQueryKeys.apps.root(sibling.id), ["sibling"]);
    queryClient.setQueryData(["global", "health"], "healthy");
    const { result } = renderController({
      canManage: true,
      deleteCommand,
      lastWorkspaceName: target.name,
      queryClient,
      rememberWorkspaceName,
      replacePath,
      workspaces: [sibling, target],
    });

    act(() => result.current.begin(target, true));
    act(() => result.current.setConfirmation(target.name));
    act(() => {
      result.current.submit();
      result.current.submit();
    });

    await waitFor(() => expect(result.current.target).toBeNull());
    expect(deleteCommand).toHaveBeenCalledOnce();
    expect(deleteCommand).toHaveBeenCalledWith(target.id);
    expect(queryClient.getQueryData(currentSessionQueryOptions().queryKey)).toEqual({
      user: sessionUser(),
      workspaces: [sibling],
    });
    expect(queryClient.getQueryData(workspaceQueryKeys.apps.root(target.id))).toBeUndefined();
    expect(queryClient.getQueryData(workspaceQueryKeys.apps.root(sibling.id))).toEqual(["sibling"]);
    expect(queryClient.getQueryData(["global", "health"])).toBe("healthy");
    expect(rememberWorkspaceName).toHaveBeenCalledWith(sibling.name);
    expect(replacePath).toHaveBeenCalledWith("/w/default/apps");
  });

  it("keeps confirmation and exact failure available for retry", async () => {
    const target = workspace("workspace-2", "research");
    const sibling = workspace("workspace-1", "default");
    const failure = new Error("cleanup acknowledgement timed out");
    const deleteCommand = vi
      .fn<(workspaceId: string) => Promise<null>>()
      .mockRejectedValueOnce(failure)
      .mockResolvedValueOnce(null);
    const { result } = renderController({
      canManage: true,
      deleteCommand,
      lastWorkspaceName: null,
      queryClient: testQueryClient(),
      rememberWorkspaceName: vi.fn(),
      replacePath: vi.fn(),
      workspaces: [sibling, target],
    });

    act(() => result.current.begin(target, false));
    act(() => result.current.setConfirmation(target.name));
    act(() => result.current.submit());

    await waitFor(() => expect(result.current.error).toBe(failure));
    expect(result.current.target?.workspace).toEqual(target);
    expect(result.current.confirmation).toBe(target.name);

    act(() => result.current.submit());
    await waitFor(() => expect(result.current.target).toBeNull());
    expect(deleteCommand).toHaveBeenCalledTimes(2);
  });

  it("blocks protected starts but allows an accepted deleting workspace to resume", () => {
    const owner = workspace("workspace-1", "default");
    const deleting = {
      ...workspace("workspace-2", "research"),
      status: "deleting" as const,
    };
    const { result } = renderController({
      canManage: true,
      deleteCommand: vi.fn(),
      lastWorkspaceName: null,
      queryClient: testQueryClient(),
      rememberWorkspaceName: vi.fn(),
      replacePath: vi.fn(),
      workspaces: [owner, deleting],
    });

    act(() => result.current.begin(owner, true));
    expect(result.current.target).toBeNull();
    expect(result.current.availability(deleting)).toEqual({ allowed: true });
    act(() => result.current.begin(deleting, true));
    expect(result.current.target?.workspace).toEqual(deleting);
  });
});

function renderController({
  queryClient,
  ...options
}: Parameters<typeof useWorkspaceDeletionController>[0] & {
  queryClient: QueryClient;
}) {
  return renderHook(() => useWorkspaceDeletionController(options), {
    wrapper: controllerWrapper(queryClient),
  });
}

function controllerWrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function sessionUser(): CurrentSession["user"] {
  return {
    id: "user-1",
    display_name: "owner",
    email: "",
    avatar_url: "",
    github_user_id: "",
    github_login: "",
    role: "administrator",
    status: "active",
    created_at: "2026-07-21T10:00:00Z",
    updated_at: "2026-07-21T10:00:00Z",
  };
}

function workspace(id: string, name: string): Workspace {
  return {
    id,
    name,
    status: "active",
    signing_key_prefix: null,
    primary_token_id: null,
    concurrency_limit_id: null,
    storage: { backend: "local", bucket: null, prefix: "" },
    labels: {},
    metadata: {},
    created_at: "2026-07-21T10:00:00Z",
    updated_at: "2026-07-21T10:00:00Z",
  };
}

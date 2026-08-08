import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentSession, Workspace } from "@/lib/api/schemas";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import { currentWorkspaceQueryOptions, updateWorkspace } from "@/lib/queries/workspace";

import { useWorkspaceIdentityController } from "./controller";

vi.mock("@/lib/queries/workspace", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/queries/workspace")>();
  return {
    ...actual,
    updateWorkspace: vi.fn(),
  };
});

const updateWorkspaceMock = vi.mocked(updateWorkspace);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("workspace identity controller", () => {
  it("normalizes one rename and replaces only matching directory and owner entries", async () => {
    const target = workspace("workspace-1", "acme");
    const sibling = workspace("workspace-2", "platform");
    const accepted = { ...target, name: "platform_team", updated_at: "2026-07-21T12:00:00Z" };
    const queryClient = testQueryClient();
    const replacePath = vi.fn();
    queryClient.setQueryData(currentSessionQueryOptions().queryKey, {
      user: sessionUser(),
      workspaces: [target, sibling],
    });
    queryClient.setQueryData(currentWorkspaceQueryOptions().queryKey, target);
    updateWorkspaceMock.mockResolvedValue(accepted);
    const { result } = renderController({ queryClient, workspace: target, replacePath });

    act(() => result.current.beginEditing());
    act(() => result.current.setDraftName("  platform_team  "));
    act(() => result.current.save());

    await waitFor(() => expect(result.current.mode).toBe("saved"));
    expect(updateWorkspaceMock).toHaveBeenCalledWith("workspace-1", "platform_team");
    expect(queryClient.getQueryData(currentSessionQueryOptions().queryKey)).toEqual({
      user: sessionUser(),
      workspaces: [accepted, sibling],
    });
    expect(queryClient.getQueryData(currentWorkspaceQueryOptions().queryKey)).toEqual(accepted);
    expect(replacePath).toHaveBeenCalledWith("/w/platform_team/settings");

    const otherOwner = workspace("workspace-owner", "owner");
    queryClient.setQueryData(currentWorkspaceQueryOptions().queryKey, otherOwner);
    updateWorkspaceMock.mockResolvedValue({ ...accepted, name: "platform-next" });
    const second = renderController({ queryClient, workspace: accepted, replacePath });
    act(() => second.result.current.beginEditing());
    act(() => second.result.current.setDraftName("platform-next"));
    act(() => second.result.current.save());

    await waitFor(() => expect(second.result.current.mode).toBe("saved"));
    expect(queryClient.getQueryData(currentWorkspaceQueryOptions().queryKey)).toEqual(otherOwner);
  });

  it("keeps a failed draft and exact error available for retry", async () => {
    const target = workspace("workspace-1", "acme");
    const failure = new Error("workspace name is already in use: platform");
    updateWorkspaceMock.mockRejectedValue(failure);
    const replacePath = vi.fn();
    const { result } = renderController({
      queryClient: testQueryClient(),
      workspace: target,
      replacePath,
    });

    act(() => result.current.beginEditing());
    act(() => result.current.setDraftName("platform"));
    act(() => result.current.save());

    await waitFor(() => expect(result.current.mode).toBe("error"));
    expect(result.current.draftName).toBe("platform");
    expect(result.current.error).toBe(failure);
    expect(result.current.canSave).toBe(true);
    expect(replacePath).not.toHaveBeenCalled();
  });

  it("rejects a duplicate submission synchronously and keeps cancel locked while saving", async () => {
    const pending = deferred<Workspace>();
    updateWorkspaceMock.mockReturnValue(pending.promise);
    const { result } = renderController({
      queryClient: testQueryClient(),
      workspace: workspace("workspace-1", "acme"),
      replacePath: vi.fn(),
    });

    act(() => result.current.beginEditing());
    act(() => result.current.setDraftName("platform"));
    act(() => {
      result.current.save();
      result.current.save();
    });

    expect(result.current.mode).toBe("saving");
    await waitFor(() => expect(updateWorkspaceMock).toHaveBeenCalledOnce());
    act(() => result.current.cancel());
    expect(result.current.mode).toBe("saving");

    pending.resolve(workspace("workspace-1", "platform"));
    await waitFor(() => expect(result.current.mode).toBe("saved"));
  });

  it("does not submit unchanged or invalid names", () => {
    const target = workspace("workspace-1", "acme");
    const { result } = renderController({
      queryClient: testQueryClient(),
      workspace: target,
      replacePath: vi.fn(),
    });

    act(() => result.current.beginEditing());
    expect(result.current.canSave).toBe(false);
    act(() => result.current.save());

    for (const invalid of ["", "   ", "Acme", "1acme", "acme team", "a".repeat(64)]) {
      act(() => result.current.setDraftName(invalid));
      expect(result.current.canSave).toBe(false);
      act(() => result.current.save());
    }

    act(() => result.current.setDraftName("  acme  "));
    expect(result.current.canSave).toBe(false);
    act(() => result.current.save());
    expect(updateWorkspaceMock).not.toHaveBeenCalled();
  });

  it("drops another workspace's draft and error when the workspace changes", async () => {
    const failure = new Error("rename failed");
    updateWorkspaceMock.mockRejectedValue(failure);
    const queryClient = testQueryClient();
    const replacePath = vi.fn();
    const { result, rerender } = renderHook(
      ({ workspaceValue }: { workspaceValue: Workspace }) =>
        useWorkspaceIdentityController({
          workspace: workspaceValue,
          replacePath,
        }),
      {
        initialProps: { workspaceValue: workspace("workspace-1", "acme") },
        wrapper: controllerWrapper(queryClient),
      },
    );
    act(() => result.current.beginEditing());
    act(() => result.current.setDraftName("platform"));
    act(() => result.current.save());
    await waitFor(() => expect(result.current.mode).toBe("error"));

    rerender({ workspaceValue: workspace("workspace-2", "research") });

    expect(result.current.mode).toBe("idle");
    expect(result.current.draftName).toBe("research");
    expect(result.current.error).toBeNull();
    expect(result.current.isEditing).toBe(false);
  });
});

function renderController({
  queryClient,
  workspace: workspaceValue,
  replacePath,
}: {
  queryClient: QueryClient;
  workspace: Workspace;
  replacePath: (path: string) => void;
}) {
  return renderHook(
    () => useWorkspaceIdentityController({ workspace: workspaceValue, replacePath }),
    { wrapper: controllerWrapper(queryClient) },
  );
}

function controllerWrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function testQueryClient() {
  return new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false },
    },
  });
}

function sessionUser(): CurrentSession["user"] {
  return {
    id: "user-1",
    username: "owner",
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

function deferred<T>() {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

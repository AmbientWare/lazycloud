import { createElement, useState, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentSession, Workspace } from "@/lib/api/schemas";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import { updateWorkspace } from "@/lib/queries/workspace";

import { useWorkspaceRenameController } from "./controller";
import { WorkspaceRenameDialog } from "./Dialog";

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

afterEach(cleanup);

describe("workspace identity controller", () => {
  it.each(["acme", "platform"])(
    "renames %s from the dialog while preserving the viewed resource's workspace",
    async (targetName) => {
      const target = workspace("workspace-1", targetName);
      const accepted = { ...target, name: "renamed" };
      const queryClient = testQueryClient();
      updateWorkspaceMock.mockResolvedValue(accepted);
      const root = createRootRoute();
      const workspaceRoute = createRoute({
        getParentRoute: () => root,
        path: "/w/$workspace",
      });
      const resourceRoute = createRoute({
        getParentRoute: () => workspaceRoute,
        path: "apps/$appId",
        component: function ResourcePage() {
          const [open, setOpen] = useState(true);
          return open
            ? createElement(WorkspaceRenameDialog, {
                workspace: target,
                onClose: () => setOpen(false),
              })
            : null;
        },
      });
      const router = createRouter({
        routeTree: root.addChildren([workspaceRoute.addChildren([resourceRoute])]),
        history: createMemoryHistory({
          initialEntries: ["/w/acme/apps/app-1?settings=workspace#details"],
        }),
      });
      render(
        createElement(
          QueryClientProvider,
          { client: queryClient },
          createElement(RouterProvider, { router }),
        ),
      );
      await act(() => router.load());

      fireEvent.change(screen.getByRole("textbox", { name: "Workspace name" }), {
        target: { value: "renamed" },
      });
      expect(screen.getByRole("button", { name: "Rename", exact: true })).toBeEnabled();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Rename", exact: true }));
      });

      const expectedWorkspace = targetName === "acme" ? "renamed" : "acme";
      expect(router.state.location.href).toBe(
        `/w/${expectedWorkspace}/apps/app-1?settings=workspace#details`,
      );
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(updateWorkspaceMock).toHaveBeenCalledWith(target.id, "renamed");
      router.history.destroy();
      queryClient.clear();
    },
  );

  it("normalizes one rename and replaces only the matching directory entry", async () => {
    const target = workspace("workspace-1", "acme");
    const sibling = workspace("workspace-2", "platform");
    const accepted = { ...target, name: "platform_team", updated_at: "2026-07-21T12:00:00Z" };
    const queryClient = testQueryClient();
    const onRenamed = vi.fn();
    queryClient.setQueryData(currentSessionQueryOptions().queryKey, {
      user: sessionUser(),
      workspaces: [target, sibling],
    });
    updateWorkspaceMock.mockResolvedValue(accepted);
    const { result } = renderController({ queryClient, workspace: target, onRenamed });

    act(() => result.current.setDraftName("  platform_team  "));
    act(() => result.current.save());

    await waitFor(() => expect(result.current.mode).toBe("saved"));
    expect(updateWorkspaceMock).toHaveBeenCalledWith("workspace-1", "platform_team");
    expect(queryClient.getQueryData(currentSessionQueryOptions().queryKey)).toEqual({
      user: sessionUser(),
      workspaces: [accepted, sibling],
    });
    expect(onRenamed).toHaveBeenCalledWith("platform_team");
  });

  it("keeps a failed draft and exact error available for retry", async () => {
    const target = workspace("workspace-1", "acme");
    const failure = new Error("workspace name is already in use: platform");
    updateWorkspaceMock.mockRejectedValue(failure);
    const onRenamed = vi.fn();
    const { result } = renderController({
      queryClient: testQueryClient(),
      workspace: target,
      onRenamed,
    });

    act(() => result.current.setDraftName("platform"));
    act(() => result.current.save());

    await waitFor(() => expect(result.current.mode).toBe("error"));
    expect(result.current.draftName).toBe("platform");
    expect(result.current.error).toBe(failure);
    expect(result.current.canSave).toBe(true);
    expect(onRenamed).not.toHaveBeenCalled();
  });

  it("rejects a duplicate submission synchronously", async () => {
    const pending = deferred<Workspace>();
    updateWorkspaceMock.mockReturnValue(pending.promise);
    const { result } = renderController({
      queryClient: testQueryClient(),
      workspace: workspace("workspace-1", "acme"),
      onRenamed: vi.fn(),
    });

    act(() => result.current.setDraftName("platform"));
    act(() => {
      result.current.save();
      result.current.save();
    });

    expect(result.current.mode).toBe("saving");
    await waitFor(() => expect(updateWorkspaceMock).toHaveBeenCalledOnce());

    pending.resolve(workspace("workspace-1", "platform"));
    await waitFor(() => expect(result.current.mode).toBe("saved"));
  });

  it("does not submit unchanged or invalid names", () => {
    const target = workspace("workspace-1", "acme");
    const { result } = renderController({
      queryClient: testQueryClient(),
      workspace: target,
      onRenamed: vi.fn(),
    });

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
    const onRenamed = vi.fn();
    const { result, rerender } = renderHook(
      ({ workspaceValue }: { workspaceValue: Workspace }) =>
        useWorkspaceRenameController({
          workspace: workspaceValue,
          onRenamed,
        }),
      {
        initialProps: { workspaceValue: workspace("workspace-1", "acme") },
        wrapper: controllerWrapper(queryClient),
      },
    );
    act(() => result.current.setDraftName("platform"));
    act(() => result.current.save());
    await waitFor(() => expect(result.current.mode).toBe("error"));

    rerender({ workspaceValue: workspace("workspace-2", "research") });

    expect(result.current.mode).toBe("editing");
    expect(result.current.draftName).toBe("research");
    expect(result.current.error).toBeNull();
  });
});

function renderController({
  queryClient,
  workspace: workspaceValue,
  onRenamed,
}: {
  queryClient: QueryClient;
  workspace: Workspace;
  onRenamed: (name: string) => void;
}) {
  return renderHook(() => useWorkspaceRenameController({ workspace: workspaceValue, onRenamed }), {
    wrapper: controllerWrapper(queryClient),
  });
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

function deferred<T>() {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

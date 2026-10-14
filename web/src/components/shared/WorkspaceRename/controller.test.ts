import { testQueryClient } from "@/test/query-client";
import { createElement, useState, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { currentSessionQueryOptions } from "@/lib/queries/auth";

import { useWorkspaceRenameController } from "./controller";
import { WorkspaceRenameDialog } from "./Dialog";

type Workspace = Schemas["Workspace"];
type Rename = { path: string; body: unknown };

/**
 * Answers each PATCH /v1/workspaces/{name} with `answer` and records it. The
 * default answer is the server's: the same workspace under the new name.
 */
function serveRenames(
  answer: (rename: Rename) => Response | Promise<Response> = (rename) =>
    Response.json(workspace("workspace-1", (rename.body as { name: string }).name)),
): Rename[] {
  const renames: Rename[] = [];
  vi.stubGlobal("fetch", async (request: Request) => {
    if (request.method !== "PATCH") throw new Error(`Unexpected ${request.method}`);
    const rename = { path: new URL(request.url).pathname, body: await request.json() };
    renames.push(rename);
    return answer(rename);
  });
  return renames;
}

describe("workspace identity controller", () => {
  it.each(["acme", "platform"])(
    "renames %s from the dialog while preserving the viewed resource's workspace",
    async (targetName) => {
      const target = workspace("workspace-1", targetName);
      const renames = serveRenames();
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
          { client: testQueryClient() },
          createElement(RouterProvider, { router }),
        ),
      );
      await act(() => router.load());

      fireEvent.change(screen.getByRole("textbox", { name: "Workspace name" }), {
        target: { value: "renamed" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Rename" }));

      const expectedWorkspace = targetName === "acme" ? "renamed" : "acme";
      await waitFor(() =>
        expect(router.state.location.href).toBe(
          `/w/${expectedWorkspace}/apps/app-1?settings=workspace#details`,
        ),
      );
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(renames).toEqual([
        { path: `/v1/workspaces/${targetName}`, body: { name: "renamed" } },
      ]);
      router.history.destroy();
    },
  );

  it("sends one trimmed rename however often it is saved, and replaces only that directory entry", async () => {
    const target = workspace("workspace-1", "acme");
    const sibling = workspace("workspace-2", "platform");
    const queryClient = testQueryClient();
    const onRenamed = vi.fn();
    queryClient.setQueryData(currentSessionQueryOptions().queryKey, {
      user: sessionUser(),
      workspaces: [target, sibling],
    });
    const renames = serveRenames();
    const { result } = renderController({ queryClient, workspace: target, onRenamed });

    act(() => result.current.setDraftName("  platform-team  "));
    act(() => {
      result.current.save();
      result.current.save();
    });
    expect(result.current.mode).toBe("saving");

    await waitFor(() => expect(result.current.mode).toBe("saved"));
    expect(renames).toEqual([{ path: "/v1/workspaces/acme", body: { name: "platform-team" } }]);
    expect(queryClient.getQueryData(currentSessionQueryOptions().queryKey)).toEqual({
      user: sessionUser(),
      workspaces: [workspace("workspace-1", "platform-team"), sibling],
    });
    expect(onRenamed).toHaveBeenCalledWith("platform-team");
  });

  it("keeps a failed draft and the server's error available for retry", async () => {
    serveRenames(() =>
      Response.json(
        { code: "conflict", message: "workspace name is already in use: platform" },
        { status: 409 },
      ),
    );
    const onRenamed = vi.fn();
    const { result } = renderController({
      queryClient: testQueryClient(),
      workspace: workspace("workspace-1", "acme"),
      onRenamed,
    });

    act(() => result.current.setDraftName("platform"));
    act(() => result.current.save());

    await waitFor(() => expect(result.current.mode).toBe("error"));
    expect(result.current.draftName).toBe("platform");
    expect(result.current.error?.message).toBe("workspace name is already in use: platform");
    expect(result.current.canSave).toBe(true);
    expect(onRenamed).not.toHaveBeenCalled();
  });

  it("does not submit unchanged or invalid names", () => {
    const renames = serveRenames();
    const { result } = renderController({
      queryClient: testQueryClient(),
      workspace: workspace("workspace-1", "acme"),
      onRenamed: vi.fn(),
    });

    expect(result.current.canSave).toBe(false);
    act(() => result.current.save());

    for (const invalid of ["", "   ", "Acme", "1acme", "acme team", "acme_team", "a".repeat(64)]) {
      act(() => result.current.setDraftName(invalid));
      expect(result.current.canSave).toBe(false);
      act(() => result.current.save());
    }

    act(() => result.current.setDraftName("  acme  "));
    expect(result.current.canSave).toBe(false);
    act(() => result.current.save());
    expect(renames).toEqual([]);
  });

  it("drops another workspace's draft and error when the workspace changes", async () => {
    serveRenames(() =>
      Response.json({ code: "unavailable", message: "rename failed" }, { status: 503 }),
    );
    const { result, rerender } = renderHook(
      ({ workspaceValue }: { workspaceValue: Workspace }) =>
        useWorkspaceRenameController({ workspace: workspaceValue, onRenamed: vi.fn() }),
      {
        initialProps: { workspaceValue: workspace("workspace-1", "acme") },
        wrapper: controllerWrapper(testQueryClient()),
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

function sessionUser(): Schemas["User"] {
  return {
    id: "user-1",
    display_name: "owner",
    email: "",
    avatar_url: "",
    github_login: "",
    is_admin: true,
    status: "active",
    created_at: "2026-07-21T10:00:00Z",
  };
}

function workspace(id: string, name: string): Workspace {
  return {
    id,
    name,
    state: "active",
    role: "owner",
    created_at: "2026-07-21T10:00:00Z",
  };
}

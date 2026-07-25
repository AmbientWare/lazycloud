import { useEffect, useMemo } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { Trash2, TriangleAlert } from "lucide-react";

import { AppShell } from "@/components/shared/AppShell";
import { useSession } from "@/components/shared/AuthGate/session";
import { WorkspaceDeletionProvider } from "@/components/shared/WorkspaceDeletion";
import { useWorkspaceDeletion } from "@/components/shared/WorkspaceDeletion/context";
import { WorkspaceLiveUpdatesProvider } from "@/components/shared/WorkspaceLiveUpdates";
import { Button } from "@/components/ui/button";
import type { Workspace } from "@/lib/api/schemas";
import { WorkspaceContext, type WorkspaceContextValue } from "@/lib/workspace-context";
import { useWorkspaceSelection } from "@/lib/workspace-selection";

export const Route = createFileRoute("/w/$workspace")({
  component: WorkspaceLayout,
});

function WorkspaceLayout() {
  const { workspace: workspaceName } = Route.useParams();
  const { workspaces } = useSession();
  const rememberWorkspaceName = useWorkspaceSelection((state) => state.rememberWorkspaceName);
  const workspace = workspaces.find((item) => item.name === workspaceName) ?? null;

  const contextValue = useMemo<WorkspaceContextValue | null>(
    () => (workspace ? { workspace, workspaces } : null),
    [workspace, workspaces],
  );

  useEffect(() => {
    if (workspace) rememberWorkspaceName(workspace.name);
  }, [rememberWorkspaceName, workspace]);

  return (
    <WorkspaceDeletionProvider>
      {!workspace || !contextValue ? (
        <WorkspaceNotFound
          workspaceName={workspaceName}
          workspaceNames={workspaces.map((item) => item.name)}
        />
      ) : workspace.status === "deleting" ? (
        <WorkspaceDeletionRecovery workspace={workspace} />
      ) : (
        <WorkspaceContext.Provider value={contextValue}>
          <WorkspaceLiveUpdatesProvider
            key={contextValue.workspace.id}
            workspaceId={contextValue.workspace.id}
          >
            <AppShell />
          </WorkspaceLiveUpdatesProvider>
        </WorkspaceContext.Provider>
      )}
    </WorkspaceDeletionProvider>
  );
}

function WorkspaceDeletionRecovery({ workspace }: { workspace: Workspace }) {
  const deletion = useWorkspaceDeletion();
  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <section className="panel w-full max-w-lg rounded-md p-5">
        <div className="flex items-start gap-3">
          <TriangleAlert
            className="mt-0.5 size-5 shrink-0 text-destructive"
            aria-hidden="true"
          />
          <div className="min-w-0 flex-1">
            <h1 className="text-lg font-semibold">
              Deletion is incomplete for {workspace.name}
            </h1>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              Normal workspace operations are paused while cleanup is in
              progress. Resume the idempotent deletion to finish cleanup, or
              switch to another workspace.
            </p>
            {deletion.canManage ? (
              <Button
                type="button"
                variant="destructive"
                className="mt-4"
                onClick={() => deletion.begin(workspace, true)}
              >
                <Trash2 />
                Resume deletion
              </Button>
            ) : (
              <p className="mt-4 text-sm text-destructive" role="alert">
                Administrator access is required to resume deletion.
              </p>
            )}
          </div>
        </div>
      </section>
    </main>
  );
}

function WorkspaceNotFound({
  workspaceName,
  workspaceNames,
}: {
  workspaceName: string;
  workspaceNames: string[];
}) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <section className="panel w-full max-w-md rounded-md p-5">
        <h1 className="text-lg font-semibold">Workspace not found</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          No workspace named <code className="mono">{workspaceName}</code> is visible to this token.
        </p>
        <div className="mt-4 space-y-1">
          {workspaceNames.map((name) => (
            <Link
              key={name}
              to="/w/$workspace/apps"
              params={{ workspace: name }}
              className="interactive-panel block rounded-md border border-border px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {name}
            </Link>
          ))}
        </div>
      </section>
    </main>
  );
}

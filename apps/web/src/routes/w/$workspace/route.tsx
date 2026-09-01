import { useEffect, useMemo } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { Trash2, TriangleAlert } from "lucide-react";

import { AppShell } from "@/components/shared/AppShell";
import { settingsView, type SettingsView } from "@/components/shared/SettingsDialog/view";
import { useSession } from "@/components/shared/AuthGate/session";
import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { WorkspaceDeletionProvider } from "@/components/shared/WorkspaceDeletion";
import { useWorkspaceDeletion } from "@/components/shared/WorkspaceDeletion/context";
import { WorkspaceLiveUpdatesProvider } from "@/components/shared/WorkspaceLiveUpdates";
import { Button } from "@/components/ui/button";
import type { Workspace } from "@/lib/api/schemas";
import { WorkspaceContext, type WorkspaceContextValue } from "@/lib/workspace-context";
import { useWorkspaceSelection } from "@/lib/workspace-selection";

export const Route = createFileRoute("/w/$workspace")({
  // Settings opens as a layer over whichever page you were on, so it is addressed by
  // a search param rather than a route: a link still reaches it, back closes it, and
  // you keep your place underneath. Declared on the layout so every page below can
  // carry it.
  validateSearch: (search: Record<string, unknown>): { settings?: SettingsView } => {
    const view = settingsView(search.settings);
    return view ? { settings: view } : {};
  },
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
    <PreShellScreen width="lg">
      <div className="flex items-start gap-3">
        <TriangleAlert className="mt-0.5 size-5 shrink-0 text-destructive" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <h1 className="text-lg font-semibold">Deletion is incomplete for {workspace.name}</h1>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            Workspace operations are paused until cleanup finishes. Resume deletion or switch
            workspaces.
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
              Only an administrator can resume deletion.
            </p>
          )}
        </div>
      </div>
    </PreShellScreen>
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
    <PreShellScreen>
      <h1 className="text-lg font-semibold">Workspace not found</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        You cannot access a workspace named <code className="mono">{workspaceName}</code>.
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
    </PreShellScreen>
  );
}

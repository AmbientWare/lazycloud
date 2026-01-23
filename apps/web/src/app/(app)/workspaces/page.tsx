"use client";

import { useState, useEffect } from "react";
import { WorkspaceSelector } from "./_components/workspace-selector";
import { WorkspaceOverview } from "./_components/workspace-overview";
import { MemberListWrapper } from "./_components/member-list";
import { PendingInvitations } from "./_components/pending-invitations";
import { getWorkspaces } from "@/actions/workspaces";
import { Building2 } from "lucide-react";
import { Spinner } from "@/components/shared/spinner";
import type { Workspace } from "@/interfaces/workspaces";

export default function WorkspacesPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(null);
  const [isContentLoading, setIsContentLoading] = useState(false);

  useEffect(() => {
    const loadInitialData = async () => {
      try {
        const workspacesData = await getWorkspaces();
        // Everyone has a Personal workspace - find and set it immediately
        const personalWorkspace = workspacesData.find((w) => w.is_personal);
        if (personalWorkspace) {
          setSelectedWorkspaceId(personalWorkspace.id);
        } else if (workspacesData.length > 0 && workspacesData[0]) {
          // Fallback to first workspace if Personal somehow doesn't exist
          setSelectedWorkspaceId(workspacesData[0].id);
        }
        setWorkspaces(workspacesData);
      } catch (error) {
        console.error("Failed to load workspaces data:", error);
      }
    };

    void loadInitialData();
  }, []);

  const handleWorkspaceChange = (workspaceId: string) => {
    // Only set loading state if switching to a different workspace
    if (workspaceId !== selectedWorkspaceId) {
      setSelectedWorkspaceId(workspaceId);
      setIsContentLoading(true);
    }
  };

  // Track when content is loading - hide spinner after delay
  useEffect(() => {
    if (isContentLoading && selectedWorkspaceId) {
      // Hide spinner after a delay to allow content to load
      const timer = setTimeout(() => {
        setIsContentLoading(false);
      }, 500);
      return () => clearTimeout(timer);
    }
  }, [isContentLoading, selectedWorkspaceId]);

  return (
    <div className="space-y-8">
      <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <div className="bg-lazycloud/10 flex h-10 w-10 items-center justify-center rounded-lg">
              <Building2 className="text-lazycloud h-5 w-5" />
            </div>
            <h1 className="text-3xl font-bold tracking-tight">Workspaces</h1>
          </div>
          <p className="text-muted-foreground ml-[52px] text-sm">
            Manage your workspaces, deployments, and team members
          </p>
        </div>
        <div className="sm:pt-1">
          <div className="flex items-center gap-2">
            {isContentLoading && selectedWorkspaceId && (
              <Spinner size="sm" />
            )}
            <WorkspaceSelector
              initialWorkspaces={workspaces}
              currentWorkspaceId={selectedWorkspaceId ?? undefined}
              onWorkspaceChange={handleWorkspaceChange}
              onWorkspacesUpdate={setWorkspaces}
            />
          </div>
        </div>
      </div>

      <div className="space-y-6">
        <PendingInvitations />
        <WorkspaceOverview workspaceId={selectedWorkspaceId} />
        {!workspaces.find((w) => w.id === selectedWorkspaceId)?.is_personal && (
          <MemberListWrapper
            workspaceId={selectedWorkspaceId}
            workspaces={workspaces}
          />
        )}
      </div>
    </div>
  );
}

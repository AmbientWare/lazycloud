"use client";

import { useState, useEffect } from "react";
import { SectionHeader } from "@/components/shared/section-header";
import { getWorkspaceWithDeployments } from "@/actions/workspaces";
import { getDeploymentStatus } from "@/actions/deployments";
import type { DeploymentWithStatus } from "@/interfaces/deployments";
import { Accordion } from "@/components/ui/accordion";
import { Rocket } from "lucide-react";
import {
  StyledCard,
  StyledCardContent,
  StyledCardHeader,
} from "@/components/shared/styled-card";
import { DeploymentsSkeleton } from "./workspaces-skeletons";
import { DeploymentCard } from "./deployment-card";

interface WorkspaceOverviewProps {
  workspaceId: string | null;
}

const DEPLOYMENTS_HEADER = {
  title: "Deployments",
  description: "View deployments, services, and volumes in this workspace",
  titleSize: "xl" as const,
};

export function WorkspaceOverview({ workspaceId }: WorkspaceOverviewProps) {
  const [deployments, setDeployments] = useState<DeploymentWithStatus[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [openAccordionValue, setOpenAccordionValue] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (!workspaceId) {
      setIsLoading(true);
      setDeployments([]);
      setOpenAccordionValue(undefined);
      return;
    }

    // Reset accordion state when workspace changes
    setOpenAccordionValue(undefined);

    let cancelled = false;

    const loadData = async () => {
      setIsLoading(true);
      try {
        const workspaceResponse = await getWorkspaceWithDeployments(workspaceId);

        if (cancelled) return;

        setDeployments(workspaceResponse.deployments ?? []);
        setIsLoading(false);
      } catch (error) {
        if (cancelled) return;
        console.error("Failed to load workspace with deployments:", error);
        setIsLoading(false);
      }
    };

    void loadData();

    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  const loadDeploymentStatus = async (deploymentId: string) => {
    // Check if we already have status for this deployment
    const deployment = deployments.find((d) => d.id === deploymentId);
    if (deployment?.status || deployment?.isLoading) {
      return; // Already loaded or loading
    }

    // Mark as loading
    setDeployments((prev) =>
      prev.map((d) =>
        d.id === deploymentId ? { ...d, isLoading: true } : d,
      ),
    );

    try {
      const statusResponse = await getDeploymentStatus(deploymentId);

      setDeployments((prev) =>
        prev.map((d) =>
          d.id === deploymentId
            ? {
                ...d,
                status: statusResponse.status,
                isLoading: false,
              }
            : d,
        ),
      );
    } catch (error) {
      console.error(
        `Failed to load status for deployment ${deploymentId}:`,
        error,
      );
      setDeployments((prev) =>
        prev.map((d) =>
          d.id === deploymentId ? { ...d, isLoading: false } : d,
        ),
      );
    }
  };

  if (!workspaceId) {
    return <DeploymentsSkeleton />;
  }

  if (isLoading && deployments.length === 0) {
    return <DeploymentsSkeleton />;
  }

  return (
    <StyledCard className="border-l-lazycloud/40 border-l-2 shadow-md" elevation={2}>
      <StyledCardHeader>
        <SectionHeader {...DEPLOYMENTS_HEADER} />
      </StyledCardHeader>
      <StyledCardContent>
        {deployments.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <div className="bg-lazycloud/10 mb-4 flex h-12 w-12 items-center justify-center rounded-lg">
              <Rocket className="text-lazycloud h-6 w-6" />
            </div>
            <h3 className="mb-2 text-lg font-semibold">No deployments yet</h3>
            <p className="text-muted-foreground max-w-md text-sm">
              Deploy your first application to see it appear here
            </p>
          </div>
        ) : (
          <Accordion
            type="single"
            collapsible
            className="w-full space-y-2"
            value={openAccordionValue}
            onValueChange={(value) => {
              setOpenAccordionValue(value);
              if (value) {
                void loadDeploymentStatus(value);
              }
            }}
          >
            {deployments.map((deployment) => (
              <DeploymentCard key={deployment.id} deployment={deployment} />
            ))}
          </Accordion>
        )}
      </StyledCardContent>
    </StyledCard>
  );
}

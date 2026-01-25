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

const STATUS_POLL_INTERVAL_MS = 5000;

export function WorkspaceOverview({ workspaceId }: WorkspaceOverviewProps) {
  const [deployments, setDeployments] = useState<DeploymentWithStatus[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [openAccordionValue, setOpenAccordionValue] = useState<string | undefined>(undefined);

  useEffect(() => {
    // Always reset accordion state when this effect runs (workspace change or remount)
    setOpenAccordionValue(undefined);

    if (!workspaceId) {
      setIsLoading(true);
      setDeployments([]);
      return;
    }

    let cancelled = false;

    const loadData = async () => {
      setIsLoading(true);
      // Clear deployments and accordion when starting to load fresh data
      setDeployments([]);
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

  // Poll for status updates when a deployment is expanded
  useEffect(() => {
    if (!openAccordionValue) return;

    const pollStatus = async () => {
      try {
        const statusResponse = await getDeploymentStatus(openAccordionValue);
        setDeployments((prev) =>
          prev.map((d) =>
            d.id === openAccordionValue
              ? { ...d, status: statusResponse.status, isLoading: false }
              : d
          )
        );
      } catch (error) {
        console.error(`Failed to poll status for deployment ${openAccordionValue}:`, error);
      }
    };

    // Initial fetch
    void pollStatus();

    // Set up polling interval
    const intervalId = setInterval(pollStatus, STATUS_POLL_INTERVAL_MS);

    return () => clearInterval(intervalId);
  }, [openAccordionValue]);

  const loadDeploymentStatus = (deploymentId: string) => {
    // Mark as loading if we don't have status yet
    const deployment = deployments.find((d) => d.id === deploymentId);
    if (!deployment?.status && !deployment?.isLoading) {
      setDeployments((prev) =>
        prev.map((d) =>
          d.id === deploymentId ? { ...d, isLoading: true } : d
        )
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
    <StyledCard className="border-l-lazycloud/40 border-l-2 shadow-md">
      <StyledCardHeader>
        <SectionHeader {...DEPLOYMENTS_HEADER} />
      </StyledCardHeader>
      <StyledCardContent>
        {deployments.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 sm:py-12 text-center">
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

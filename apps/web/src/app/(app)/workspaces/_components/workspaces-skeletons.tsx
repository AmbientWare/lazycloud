import { Skeleton } from "@/components/ui/skeleton";
import {
  StyledCard,
  StyledCardContent,
  StyledCardHeader,
} from "@/components/shared/styled-card";
import { SectionHeader } from "@/components/shared/section-header";

const DEPLOYMENTS_HEADER = {
  title: "Deployments",
  description: "View deployments, services, and volumes in this workspace",
  titleSize: "xl" as const,
};

export function DeploymentsSkeleton() {
  return (
    <StyledCard>
      <StyledCardHeader>
        <SectionHeader {...DEPLOYMENTS_HEADER} />
      </StyledCardHeader>
      <StyledCardContent>
        <div className="space-y-3">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      </StyledCardContent>
    </StyledCard>
  );
}

export function MembersSkeleton() {
  return (
    <StyledCard>
      <StyledCardHeader>
        <SectionHeader
          title="Members"
          description="Manage workspace members and their roles"
          titleSize="xl"
        />
      </StyledCardHeader>
      <StyledCardContent>
        <div className="space-y-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      </StyledCardContent>
    </StyledCard>
  );
}


import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";

import { deploymentKindSchema } from "@/lib/api/schemas/deployments";

export function WorkloadLink({
  workspaceName,
  appId,
  name,
  kind,
  children,
  className,
  title,
}: {
  workspaceName: string;
  appId: string | null | undefined;
  name: string;
  kind: string;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  const deploymentKind = deploymentKindSchema.safeParse(kind);
  if (!appId || !deploymentKind.success) {
    return (
      <span className={className} title={title}>
        {children}
      </span>
    );
  }
  return (
    <Link
      to="/w/$workspace/apps/$appId/workloads/$name"
      params={{ workspace: workspaceName, appId, name }}
      search={{ kind: deploymentKind.data }}
      className={className}
      title={title}
    >
      {children}
    </Link>
  );
}

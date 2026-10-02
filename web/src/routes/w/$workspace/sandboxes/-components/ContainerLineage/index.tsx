import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";

import { StubKindIcon } from "@/components/shared/StubKindIcon";
import type { Schemas } from "@/lib/api/client";

export function ContainerLineage({
  record,
  workspaceName,
}: {
  record: Schemas["Container"];
  workspaceName: string;
}) {
  return (
    <nav
      aria-label="Container lineage"
      className="mt-1.5 flex flex-wrap items-center gap-1 text-xs"
    >
      <Link
        to="/w/$workspace/apps/$app"
        params={{ workspace: workspaceName, app: record.app }}
        className="text-brand hover:underline"
      >
        {record.app}
      </Link>
      <ChevronRight className="size-3 text-muted-foreground" />
      {record.kind ? (
        <Link
          to="/w/$workspace/apps/$app/workloads/$kind/$name"
          params={{
            workspace: workspaceName,
            app: record.app,
            kind: record.kind,
            name: record.function,
          }}
          className="inline-flex items-center gap-1.5 text-brand hover:underline"
        >
          <StubKindIcon kind={record.kind} className="size-3.5" />
          {record.function}
        </Link>
      ) : (
        <span className="inline-flex items-center gap-1.5 text-muted-foreground">
          {record.function}
        </span>
      )}
    </nav>
  );
}

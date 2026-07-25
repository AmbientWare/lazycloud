import { lazy, Suspense, useState } from "react";
import { MoreHorizontal } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { App } from "@/lib/api/schemas";

const AppCardActions = lazy(() =>
  import("./AppCardActions").then((module) => ({ default: module.AppCardActions })),
);

export function AppCardActionsTrigger({
  app,
  workspaceId,
}: {
  app: App;
  workspaceId: string;
}) {
  const [loaded, setLoaded] = useState(false);
  const trigger = (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="size-7 bg-card/85"
      aria-label={`Open actions for ${app.name}`}
      title="App actions"
      aria-busy={loaded || undefined}
      onClick={() => setLoaded(true)}
    >
      <MoreHorizontal />
    </Button>
  );

  if (!loaded) return trigger;

  return (
    <Suspense fallback={trigger}>
      <AppCardActions
        app={app}
        workspaceId={workspaceId}
        actionIcon={<MoreHorizontal />}
        defaultOpen
      />
    </Suspense>
  );
}

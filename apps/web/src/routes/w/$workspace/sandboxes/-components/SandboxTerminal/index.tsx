import { useEffect } from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { Terminal } from "@/components/shared/Terminal";
import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { createContainerShell, shellWebSocketUrl } from "@/lib/queries/shells";
import { useWorkspace } from "@/lib/workspace-context";

/**
 * Terminal-first sandbox view: provisions a shell into the running container
 * and renders the interactive terminal inline (not in a slide-over).
 */
export function SandboxTerminal({
  containerId,
  className,
}: {
  containerId: string;
  className?: string;
}) {
  const { workspace } = useWorkspace();
  const session = useMutation({
    mutationFn: () => createContainerShell(workspace.id, containerId),
  });
  const mutate = session.mutate;
  useEffect(() => {
    mutate();
  }, [mutate, containerId]);

  if (session.isPending || session.isIdle) {
    return (
      <div className="flex h-64 items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        Starting shell server…
      </div>
    );
  }
  if (session.isError) {
    return (
      <ApiErrorNotice
        title="Could not start the shell"
        error={session.error}
        onRetry={() => session.mutate()}
      />
    );
  }
  return (
    <Terminal
      key={session.submittedAt}
      onReconnect={() => session.mutate()}
      socketUrl={shellWebSocketUrl(
        session.data.stub_id,
        containerId,
        session.data.websocket_ticket,
      )}
      credentials={{
        username: session.data.username,
        password: session.data.password,
      }}
      className={className}
    />
  );
}

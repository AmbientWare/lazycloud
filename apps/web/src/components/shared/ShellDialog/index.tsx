import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { useMutation } from "@tanstack/react-query";
import { TerminalSquare, X } from "lucide-react";

import { PanelErrorBoundary } from "@/components/shared/ErrorBoundary";
import { Terminal } from "@/components/shared/Terminal";
import { Button } from "@/components/ui/button";
import { createContainerShell, shellWebSocketUrl } from "@/lib/queries/shells";
import { useWorkspace } from "@/lib/workspace-context";

type ShellButtonProps = {
  containerId: string;
  /** Gate the button on a running container; disabled otherwise. */
  running: boolean;
  className?: string;
  variant?: "default" | "outline" | "ghost";
  size?: "sm" | "md";
};

/** Opens the shared centered terminal for any server-authorized container. */
export function ShellButton({
  containerId,
  running,
  className,
  variant = "outline",
  size = "sm",
}: ShellButtonProps) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <Button
          variant={variant}
          size={size}
          className={className}
          disabled={!running}
          title={running ? "Open a shell into this container" : "Container is not running"}
        >
          <TerminalSquare className="size-3.5" />
          Shell
        </Button>
      </Dialog.Trigger>
      {open ? <ShellDialog containerId={containerId} /> : null}
    </Dialog.Root>
  );
}

function ShellDialog({ containerId }: { containerId: string }) {
  const { workspace } = useWorkspace();
  const session = useMutation({
    mutationFn: () => createContainerShell(workspace.id, containerId),
  });

  const mutate = session.mutate;
  useEffect(() => {
    mutate();
  }, [mutate]);

  return (
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-[60] bg-black/60 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
      <Dialog.Content className="fixed left-1/2 top-1/2 z-[60] flex h-[min(46rem,calc(100dvh-1rem))] w-[calc(100vw-1rem)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-md border border-border bg-background shadow-2xl outline-none duration-200 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 sm:h-[min(80dvh,46rem)] sm:w-[min(92vw,72rem)]">
        <header className="flex min-h-14 shrink-0 items-center gap-3 border-b border-border bg-card px-4 py-3 pr-12">
          <TerminalSquare className="size-4 shrink-0 text-brand" aria-hidden="true" />
          <Dialog.Title className="text-base font-semibold text-foreground">Shell</Dialog.Title>
          <Dialog.Description className="sr-only">Interactive terminal session</Dialog.Description>
        </header>

        <div className="flex min-h-0 flex-1 flex-col bg-background p-3 sm:p-4">
          {session.isPending ? (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              Starting shell server…
            </div>
          ) : session.isError ? (
            <div
              className="m-auto w-full max-w-xl rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
              role="alert"
            >
              {session.error.message}
            </div>
          ) : session.data ? (
            <PanelErrorBoundary key={containerId} title="Terminal could not be displayed">
              <Terminal
                socketUrl={shellWebSocketUrl(
                  session.data.stub_id,
                  containerId,
                  session.data.websocket_ticket,
                )}
                credentials={{
                  username: session.data.username,
                  password: session.data.password,
                }}
                className="h-full min-h-0 flex-1"
              />
            </PanelErrorBoundary>
          ) : null}
        </div>

        <Dialog.Close className="absolute right-3 top-3 rounded-md p-1 text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
          <X className="size-4" aria-hidden="true" />
          <span className="sr-only">Close</span>
        </Dialog.Close>
      </Dialog.Content>
    </Dialog.Portal>
  );
}

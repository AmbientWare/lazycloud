import { Boxes, TerminalSquare } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { useWorkspaceLiveUpdates } from "@/lib/workspace-context";

const QUICKSTART_STEPS = [
  {
    title: "Install and sign in",
    detail: "The login command prints a code to approve in this browser.",
    commands: ["uv tool install lazycloud", "lazycloud login"],
  },
  {
    title: "Create and deploy",
    detail: "Quickstart writes quickstart.py with one function.",
    commands: ["lazycloud quickstart", "lazycloud deploy quickstart.py:hello"],
  },
  {
    title: "Run the function",
    detail: "The first task will appear here with its output and logs.",
    commands: ["lazycloud run quickstart.py:hello 'LazyCloud'"],
  },
] as const;

/**
 * First-run guided quickstart shown while a workspace has zero apps. The
 * workspace-owned live updates flip this page to the populated grid as soon
 * as the first deployment lands.
 */
export function QuickstartEmptyState() {
  const { status: streamStatus } = useWorkspaceLiveUpdates();

  return (
    <section className="panel grid min-h-[34rem] overflow-hidden rounded-md lg:grid-cols-[minmax(0,4fr)_minmax(0,6fr)]">
      <div className="flex min-w-0 flex-col justify-between gap-12 p-6 sm:p-8 lg:p-10">
        <div>
          <div className="flex size-11 items-center justify-center rounded-md border border-brand/25 bg-brand/10 text-brand">
            <Boxes className="size-5" aria-hidden="true" />
          </div>
          <p className="micro-label mt-8">First deployment</p>
          <h2 className="mt-2 max-w-md text-2xl font-semibold tracking-tight text-foreground">
            Deploy your first app
          </h2>
          <p className="mt-3 max-w-md text-sm leading-6 text-muted-foreground">
            Quickstart writes a Python function, deploys it, and gives you a task to inspect. Run
            these commands in a terminal to get started.
          </p>
        </div>

        <div className="border-t border-border pt-4" aria-live="polite">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span
              className={`size-1.5 shrink-0 rounded-full ${
                streamStatus === "open"
                  ? "animate-pulse bg-positive motion-reduce:animate-none"
                  : "bg-muted-foreground/60"
              }`}
              aria-hidden="true"
            />
            {streamStatus === "open"
              ? "Listening for your first deployment"
              : "Connecting live updates"}
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            This page will switch to your app list when the deployment arrives.
          </p>
        </div>
      </div>

      <div className="min-w-0 border-t border-border bg-muted/20 p-4 sm:p-6 lg:border-l lg:border-t-0 lg:p-8">
        <div className="flex items-center gap-2 text-xs font-medium text-foreground">
          <TerminalSquare className="size-4 text-brand" aria-hidden="true" />
          Terminal quickstart
        </div>

        <ol className="mt-4 overflow-hidden rounded-md border border-border bg-card">
          {QUICKSTART_STEPS.map((step, index) => (
            <li
              key={step.title}
              className="grid gap-3 border-b border-border p-4 last:border-b-0 sm:grid-cols-[1.75rem_minmax(0,1fr)] sm:p-5"
            >
              <span className="mono flex size-7 items-center justify-center rounded-full border border-border bg-muted/30 text-[11px] text-muted-foreground">
                {String(index + 1).padStart(2, "0")}
              </span>
              <div className="min-w-0">
                <h3 className="text-sm font-medium text-foreground">{step.title}</h3>
                <p className="mt-0.5 text-xs leading-5 text-muted-foreground">{step.detail}</p>
                <div className="mt-3 divide-y divide-border overflow-hidden rounded-md border border-border bg-muted/35">
                  {step.commands.map((command) => (
                    <div key={command} className="flex min-w-0 items-center gap-2 px-3 py-2">
                      <span className="mono shrink-0 text-xs text-brand" aria-hidden="true">
                        $
                      </span>
                      <code className="mono min-w-0 flex-1 break-words text-xs leading-5 text-foreground">
                        {command}
                      </code>
                      <CopyButton value={command} label="command" className="size-7 shrink-0" />
                    </div>
                  ))}
                </div>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

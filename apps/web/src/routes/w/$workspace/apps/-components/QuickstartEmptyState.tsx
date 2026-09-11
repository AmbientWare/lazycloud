import { Boxes, TerminalSquare } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";

const QUICKSTART_STEPS = [
  {
    title: "Install and sign in",
    detail: "The login command prints a code to approve in this browser.",
    commands: ["uv tool install lazycloud-client", "lazycloud login"],
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

export function QuickstartEmptyState() {
  return (
    <section className="panel grid h-full min-h-[34rem] overflow-hidden rounded-md lg:grid-cols-[minmax(0,4fr)_minmax(0,6fr)]">
      <div className="flex min-w-0 flex-col justify-between gap-12 p-6 sm:p-8 lg:p-10">
        <div>
          <div className="flex size-11 items-center justify-center rounded-md border border-brand/25 bg-brand/10 text-brand">
            <Boxes className="size-5" aria-hidden="true" />
          </div>
          <h2 className="mt-8 max-w-md text-2xl font-semibold tracking-tight text-foreground">
            Deploy your first app
          </h2>
          <p className="mt-3 max-w-md text-sm leading-6 text-muted-foreground">
            Run these commands in your terminal to deploy and call a Python function.
          </p>
        </div>
      </div>

      <div className="min-w-0 border-t border-border bg-muted/20 p-4 sm:p-6 lg:border-l lg:border-t-0 lg:p-8">
        <div className="flex items-center gap-2 text-xs font-medium text-foreground">
          <TerminalSquare className="size-4 text-brand" aria-hidden="true" />
          Quickstart
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

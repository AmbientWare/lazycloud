import { CopyButton } from "@/components/shared/CopyButton";
import { Panel } from "@/components/shared/Panel";

const QUICKSTART_STEPS = [
  {
    title: "Install and sign in",
    commands: ["uv tool install lazycloud-client", "lazycloud login"],
  },
  {
    title: "Download the project",
    commands: ["lazycloud example download quickstart", "cd quickstart", "uv sync"],
  },
  {
    title: "Run the function",
    commands: ["uv run lazycloud run quickstart:hello 'LazyCloud'"],
  },
] as const;

export function QuickstartEmptyState() {
  return (
    <Panel title="Run your first function" className="h-full" contentClassName="p-0">
      <ol>
        {QUICKSTART_STEPS.map((step, index) => (
          <li
            key={step.title}
            className="grid grid-cols-[1.75rem_minmax(0,1fr)] gap-3 border-b border-border p-3 last:border-b-0"
          >
            <span className="mono flex size-7 items-center justify-center rounded-md border border-border bg-muted/30 text-[11px] text-muted-foreground">
              {String(index + 1).padStart(2, "0")}
            </span>
            <div className="min-w-0">
              <h3 className="text-sm font-medium text-foreground">{step.title}</h3>
              <div className="mt-2 divide-y divide-border overflow-hidden rounded-md border border-border bg-muted/35">
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
    </Panel>
  );
}

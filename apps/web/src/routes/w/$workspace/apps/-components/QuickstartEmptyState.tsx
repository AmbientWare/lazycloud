import { CliHint } from "@/components/shared/CliHint";
import { useWorkspaceLiveUpdates } from "@/lib/workspace-context";

/**
 * First-run guided quickstart shown while a workspace has zero apps. The
 * workspace-owned live updates flip this page to the populated grid as soon
 * as the first deployment lands.
 */
export function QuickstartEmptyState() {
  const { status: streamStatus } = useWorkspaceLiveUpdates();

  const steps: { title: string; detail: string; commands: string[] }[] = [
    {
      title: "Install the client",
      detail: "Install the public LazyCloud client with uv.",
      commands: ["uv tool install lazycloud"],
    },
    {
      title: "Sign in from your terminal",
      detail: "Approve the printed code in this browser when prompted.",
      commands: ["lazycloud login"],
    },
    {
      title: "Create and deploy the starter app",
      detail: "Writes quickstart.py with one function, then deploys it here.",
      commands: [
        "lazycloud quickstart",
        "lazycloud deploy quickstart.py:hello",
      ],
    },
    {
      title: "Run and inspect it",
      detail: "Invoke the function, then inspect its task output and logs.",
      commands: [
        "lazycloud run quickstart.py:hello 'LazyCloud'",
        "lazycloud task list",
        "lazycloud task result <task-id>",
        "lazycloud task logs <task-id>",
      ],
    },
  ];

  return (
    <div className="mx-auto w-full max-w-lg py-10">
      <h3 className="text-base font-semibold">Deploy your first app</h3>
      <p className="mt-1 text-sm text-muted-foreground">
        This workspace has no apps yet. Two commands get one running.
      </p>

      <ol className="mt-6 space-y-5">
        {steps.map((step, index) => (
          <li key={step.title} className="flex gap-3">
            <span className="mono mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border border-border text-xs text-muted-foreground">
              {index + 1}
            </span>
            <div className="min-w-0 flex-1 space-y-2">
              <div>
                <div className="text-sm font-medium">{step.title}</div>
                <div className="text-xs text-muted-foreground">{step.detail}</div>
              </div>
              {step.commands.map((command) => (
                <CliHint key={command} command={command} />
              ))}
            </div>
          </li>
        ))}
        <li className="flex gap-3">
          <span className="mono mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border border-border text-xs text-muted-foreground">
            {steps.length + 1}
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium">Watch it appear</div>
            <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
              <span
                className={`size-1.5 rounded-full ${
                  streamStatus === "open" ? "animate-pulse bg-positive" : "bg-muted-foreground/60"
                }`}
                aria-hidden="true"
              />
              {streamStatus === "open"
                ? "Watching for your first deployment — this page updates automatically."
                : "Connecting live updates."}
            </div>
          </div>
        </li>
      </ol>
    </div>
  );
}

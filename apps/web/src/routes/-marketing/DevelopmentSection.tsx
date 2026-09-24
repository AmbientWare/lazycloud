import { Link } from "@tanstack/react-router";
import { ArrowUpRight, Box, Code2, GitBranch, Globe, Layers, Terminal } from "lucide-react";

import { DOCS_URL } from "@/lib/env";

import { MarketingCard, shell } from "./MarketingPrimitives";

const agents = ["Codex", "Claude Code", "OpenCode"];
const workloads = [
  { name: "APIs", icon: Globe },
  { name: "Jobs", icon: GitBranch },
  { name: "Sandboxes", icon: Box },
];

const detailLink =
  "mt-6 inline-flex min-h-11 w-fit items-center gap-2 rounded-sm text-sm font-semibold text-brand outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function DevelopmentSection() {
  return (
    <section
      id="devboxes"
      aria-labelledby="development-title"
      className="scroll-mt-24 border-t border-input bg-background py-18 sm:py-22 lg:py-28"
    >
      <div className={shell}>
        <h2
          id="development-title"
          className="mx-auto mb-10 max-w-[760px] text-center text-[clamp(2.125rem,5vw,3.625rem)] leading-[1.08] font-[550] tracking-[-0.045em] text-balance sm:mb-14"
        >
          Build with agents.
          <br />
          Ship with LazyCloud.
        </h2>

        <div className="grid gap-8 md:grid-cols-2 md:gap-6 lg:gap-8">
          <article className="flex min-w-0 flex-col" aria-labelledby="devboxes-title">
            <div className="px-2 pb-7 sm:px-6 sm:pb-9">
              <div className="mx-auto flex w-fit items-center gap-2 rounded-md border border-border bg-background-subtle px-4 py-2.5 text-sm">
                <Layers className="size-4 text-brand" aria-hidden="true" />
                Shared image and tools
              </div>
              <Branches />
              <div className="grid grid-cols-3 gap-2 sm:gap-3">
                {agents.map((agent) => (
                  <div
                    key={agent}
                    className="surface-inset min-w-0 rounded-md border px-2 py-4 text-center sm:py-5"
                  >
                    <Terminal className="mx-auto mb-3 size-5 text-brand" aria-hidden="true" />
                    <p className="text-xs font-medium sm:text-sm">{agent}</p>
                    <p className="mt-1.5 text-[11px] text-muted-foreground">Own dev box</p>
                  </div>
                ))}
              </div>
            </div>

            <MarketingCard surface="frame" className="flex flex-1 flex-col p-6 sm:p-8">
              <h3 id="devboxes-title" className="text-2xl font-medium tracking-[-0.025em]">
                Dev boxes
              </h3>
              <p className="mt-2 text-base text-foreground">A workspace for every coding agent.</p>
              <p className="mt-4 text-sm leading-relaxed text-muted-foreground">
                Run Codex, Claude Code, OpenCode, or your own harness in parallel. Define your tools
                and dependencies once, then provision separate boxes from the same image with one
                deploy command.
              </p>
              <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
                Connect over SSH from your terminal or editor. Each box has its own persistent disk,
                so repos and installed packages survive stops and restarts.
              </p>
              <div className="mt-auto">
                <a
                  href={`${DOCS_URL.replace(/\/$/, "")}/concepts/dev-machines`}
                  className={detailLink}
                >
                  Set up dev boxes
                  <ArrowUpRight className="size-4" aria-hidden="true" />
                </a>
              </div>
            </MarketingCard>
          </article>

          <article className="flex min-w-0 flex-col" aria-labelledby="workloads-title">
            <div className="px-2 pb-7 sm:px-6 sm:pb-9">
              <div className="mx-auto flex w-fit items-center gap-2 rounded-md border border-border bg-background-subtle px-4 py-2.5 text-sm">
                <Code2 className="size-4 text-brand" aria-hidden="true" />
                Your application
              </div>
              <Branches />
              <div className="grid grid-cols-3 gap-2 sm:gap-3">
                {workloads.map(({ name, icon: Icon }) => (
                  <div
                    key={name}
                    className="surface-inset min-w-0 rounded-md border px-2 py-4 text-center sm:py-5"
                  >
                    <Icon className="mx-auto mb-3 size-5 text-brand" aria-hidden="true" />
                    <p className="text-xs font-medium sm:text-sm">{name}</p>
                    <p className="mt-1.5 text-[11px] text-muted-foreground">Cloud workload</p>
                  </div>
                ))}
              </div>
            </div>

            <MarketingCard surface="frame" className="flex flex-1 flex-col p-6 sm:p-8">
              <h3 id="workloads-title" className="text-2xl font-medium tracking-[-0.025em]">
                Workloads
              </h3>
              <p className="mt-2 text-base text-foreground">Put what you build to work.</p>
              <p className="mt-4 text-sm leading-relaxed text-muted-foreground">
                Deploy APIs and services, run background jobs, schedule tasks, and give agents
                sandboxes to execute code. Define workloads in Python alongside your application.
              </p>
              <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
                Develop locally, test in the cloud, and deploy from the same definition. Follow runs
                with live logs and saved results.
              </p>
              <div className="mt-auto">
                <Link
                  to="/"
                  hash="workloads"
                  className={detailLink}
                  onClick={() =>
                    document.getElementById("workloads")?.scrollIntoView({ block: "start" })
                  }
                >
                  Explore workloads
                  <ArrowUpRight className="size-4" aria-hidden="true" />
                </Link>
              </div>
            </MarketingCard>
          </article>
        </div>
      </div>
    </section>
  );
}

function Branches() {
  return (
    <div className="relative mx-auto h-10 w-[calc(66.6667%_+_0.25rem)]" aria-hidden="true">
      <div className="absolute top-0 bottom-0 left-1/2 border-l border-border" />
      <div className="absolute inset-x-0 top-1/2 bottom-0 rounded-t-md border-x border-t border-border" />
    </div>
  );
}

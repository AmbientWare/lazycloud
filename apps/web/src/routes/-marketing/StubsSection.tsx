import { useEffect, useRef, useState } from "react";

import { CodeBlock } from "@/components/ui/code-block";

import { MarketingCard, SectionHeading, StatusDot, shell } from "./MarketingPrimitives";
import {
  GeneratedPackagePanel,
  TypedImportPanel,
  typedClientPhase,
  useTypedClientClock,
} from "./TypedClientPreview";

/* Define the workload, generate the client package, then import and call it
   from another codebase. The definition stays a code block because the deployed
   source really is the contract; the other phases show the generated package
   and the editor that consumes it. Every symbol mirrors what the SDK decorators
   accept, what `client get` writes, and what the generated methods return. */

const defineExample = `from lazycloud import App
from pydantic import BaseModel

app = App("review_app")

class Review(BaseModel):
    summary: str
    risks: list[str]

@app.endpoint(route="/review")
def review_patch(diff: str) -> Review:
    return analyze(diff)

@app.function(retries=3)
def run_checks(commit_sha: str) -> dict[str, bool]:
    return check_release(commit_sha)`;

const phaseLabel =
  "font-mono text-sm leading-none font-semibold transition-colors duration-500 motion-reduce:transition-none sm:text-base";

/* The active phase brightens so the eye follows the sequence in order. */
function PhaseHeader({
  title,
  caption,
  active,
}: {
  title: string;
  caption: string;
  active: boolean;
}) {
  return (
    <div className="mb-2 flex flex-wrap items-baseline gap-x-2.5 gap-y-1 sm:mb-3 sm:gap-x-3">
      <h3 className={`${phaseLabel} ${active ? "text-foreground" : "text-muted-foreground"}`}>
        {title}
      </h3>
      <span className="min-w-0 font-mono text-xs leading-snug text-muted-foreground sm:text-sm">
        {caption}
      </span>
    </div>
  );
}

function StubsStory({ active }: { active: boolean }) {
  const clock = useTypedClientClock(active);
  const phase = typedClientPhase(clock);

  return (
    <div className={shell}>
      <div className="max-w-[760px]">
        <SectionHeading
          title={
            <>
              Generate a typed client <em>for your app.</em>
            </>
          }
          body="Generate a Python package with typed methods and return values for your deployed endpoints."
        />
      </div>

      <div className="grid grid-cols-[1.02fr_0.98fr] gap-4 sm:gap-6 lg:gap-7 max-lg:grid-cols-1">
        <div className="flex min-w-0 flex-col">
          <PhaseHeader active={phase === "define"} caption="your app" title="Define" />
          <MarketingCard asChild>
            <CodeBlock
              className={`flex flex-1 flex-col transition-colors duration-500 motion-reduce:transition-none ${
                phase === "define" ? "border-brand/45" : "border-input"
              }`}
              bodyClassName="flex-1 p-3 text-[10px] leading-[1.6] sm:p-5 sm:text-[11px] sm:leading-[1.7] lg:p-6 lg:text-[11.5px] lg:leading-[1.75]"
              tone="paper"
              footer={
                <div
                  className="flex min-h-11 min-w-0 flex-wrap items-center gap-x-2 gap-y-1 py-2"
                  data-marketing-terminal-surface=""
                >
                  <span className="text-brand">$</span>
                  <span className="min-w-0 flex-1 break-words [overflow-wrap:anywhere]">
                    lazycloud deploy review_app.py:app
                  </span>
                  <strong className="ml-auto inline-flex shrink-0 items-center gap-1.5 font-medium text-positive">
                    <StatusDot /> 2 workloads deployed
                  </strong>
                </div>
              }
            >
              {defineExample}
            </CodeBlock>
          </MarketingCard>
        </div>

        <div className="flex min-w-0 flex-col gap-4 sm:gap-5 lg:gap-7">
          <div className="flex min-w-0 flex-col">
            <PhaseHeader active={phase === "generate"} caption="a pinned client" title="Generate" />
            <GeneratedPackagePanel active={phase === "generate"} clock={clock} />
          </div>

          <div className="flex min-w-0 flex-col">
            <PhaseHeader
              active={phase === "import"}
              caption="from another project"
              title="Import"
            />
            <TypedImportPanel active={phase === "import"} clock={clock} />
          </div>
        </div>
      </div>
    </div>
  );
}

export function StubsSection() {
  const sectionRef = useRef<HTMLElement>(null);
  const [active, setActive] = useState(false);

  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;

    const observer = new IntersectionObserver(([entry]) => setActive(entry.isIntersecting), {
      /* Begin once the section has clearly entered the reading area, not
           while it is still below the fold during initial page load. */
      rootMargin: "0px 0px -15% 0px",
      threshold: 0.1,
    });
    observer.observe(section);
    return () => observer.disconnect();
  }, []);

  return (
    <section
      className="border-t border-input bg-muted py-14 sm:py-20 lg:py-28"
      data-animation-state={active ? "running" : "reset"}
      ref={sectionRef}
    >
      {/* The key makes each visibility boundary a lifecycle boundary: leaving
          discards the clock, and re-entering mounts a new run at frame zero. */}
      <StubsStory active={active} key={active ? "active" : "reset"} />
    </section>
  );
}

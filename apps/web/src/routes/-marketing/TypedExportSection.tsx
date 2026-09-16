import { useEffect, useRef, useState } from "react";

import { CodeBlock } from "@/components/ui/code-block";

import { MarketingCard, SectionHeading, StatusDot, shell } from "./MarketingPrimitives";
import { MarketingReveal } from "./MarketingReveal";
import {
  GeneratedPackagePanel,
  TypedImportPanel,
  typedClientPhase,
  useTypedClientClock,
} from "./TypedClientPreview";

const defineExample = `from lazycloud import App
from pydantic import BaseModel

app = App("review_app")

class Review(BaseModel):
    summary: str
    risks: list[str]

@app.endpoint(route="/review")
def review_patch(diff: str) -> Review:
    added = [line[1:] for line in diff.splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    risks = ["Review added TODOs"] if any("TODO" in line for line in added) else []
    return Review(summary=f"{len(added)} added lines", risks=risks)

@app.function(retries=3)
def run_checks(commit_sha: str) -> dict[str, bool]:
    valid = len(commit_sha) == 40 and all(c in "0123456789abcdef" for c in commit_sha)
    return {"valid_commit_sha": valid}`;

function PhaseHeader({ title, caption }: { title: string; caption: string }) {
  return (
    <div className="mb-2 flex flex-wrap items-baseline gap-x-2.5 gap-y-1 sm:mb-3 sm:gap-x-3">
      <h3 className="text-xl leading-none font-medium tracking-[-0.025em] text-foreground">
        {title}
      </h3>
      <span className="min-w-0 text-sm leading-snug text-muted-foreground">{caption}</span>
    </div>
  );
}

function TypedExportStory({ active }: { active: boolean }) {
  const clock = useTypedClientClock(active);
  const phase = typedClientPhase(clock);

  return (
    <div className={shell}>
      <div className="max-w-[760px]">
        <SectionHeading
          title={
            <>
              Export a typed package <em>for your app.</em>
            </>
          }
          body="Call deployed functions, endpoints, and ASGI routes from Python with autocomplete and type checking. ASGI route types come from OpenAPI."
        />
      </div>

      <div className="grid grid-cols-[1.02fr_0.98fr] gap-4 sm:gap-6 lg:gap-7 max-lg:grid-cols-1">
        <MarketingReveal className="relative flex min-w-0 flex-col">
          <PhaseHeader caption="your app" title="Define" />
          <MarketingCard
            surface="frame"
            className="typed-source-frame flex flex-1 flex-col p-4 sm:p-5"
          >
            <div className="mb-4 flex justify-between font-mono text-[11px] text-muted-foreground">
              <span className="text-foreground">review_app.py</span>
              <span>Python</span>
            </div>
            <MarketingCard surface="inset" asChild>
              <CodeBlock
                className="flex flex-1 flex-col"
                bodyClassName="flex-1 p-3 text-[11px] leading-[1.9] sm:p-4 sm:text-[12px]"
                footer={
                  <div
                    className="flex min-h-11 min-w-0 flex-wrap items-center gap-x-2 gap-y-1 py-2"
                    data-marketing-terminal-surface=""
                  >
                    <span className="text-brand">$</span>
                    <span className="min-w-0 flex-1 break-words [overflow-wrap:anywhere]">
                      uv run lazycloud deploy review_app:app
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
          </MarketingCard>
        </MarketingReveal>

        <div className="flex min-w-0 flex-col gap-4 sm:gap-5 lg:gap-7">
          <MarketingReveal className="relative flex min-w-0 flex-col" delay={80}>
            <PhaseHeader caption="a typed package" title="Generate" />
            <GeneratedPackagePanel active={phase === "generate"} clock={clock} />
          </MarketingReveal>

          <MarketingReveal className="relative flex min-w-0 flex-col" delay={140}>
            <PhaseHeader caption="from another project" title="Import" />
            <TypedImportPanel active={phase === "import"} clock={clock} />
          </MarketingReveal>
        </div>
      </div>
    </div>
  );
}

export function TypedExportSection() {
  const sectionRef = useRef<HTMLElement>(null);
  const [active, setActive] = useState(false);

  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;

    const observer = new IntersectionObserver(([entry]) => setActive(entry.isIntersecting), {
      rootMargin: "0px 0px -15% 0px",
      threshold: 0.1,
    });
    observer.observe(section);
    return () => observer.disconnect();
  }, []);

  return (
    <section
      id="typed-export"
      className="border-t border-input bg-background py-14 sm:py-20 lg:py-28"
      data-animation-state={active ? "running" : "paused"}
      ref={sectionRef}
    >
      <TypedExportStory active={active} />
    </section>
  );
}

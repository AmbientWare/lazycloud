import { createLazyFileRoute } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EXAMPLES_URL } from "@/lib/env";
import { cn } from "@/lib/utils";

import { MarketingLayout } from "./-marketing/MarketingLayout";
import { StubsSection } from "./-marketing/StubsSection";
import {
  FinalCta,
  Glyph,
  GetStartedButton,
  Pill,
  SectionHeading,
  StatusDot,
  shell,
} from "./-marketing/MarketingPrimitives";
import {
  ComputePlacementPreview,
  StoryPreview,
  type StoryVisual,
} from "./-marketing/ProductPreviews";
import { MarketingExampleImage } from "./-marketing/MarketingExampleImage";
import { marketingUseCases } from "./-marketing/marketingUseCases";
import { GpuPlate, LocalPlate, ProductionPlate } from "./-marketing/ParityFigures";

export const Route = createLazyFileRoute("/")({
  component: MarketingHome,
});

const endpointExample = `from lazycloud import App
from pydantic import BaseModel

app = App("review_api")

class Review(BaseModel):
    summary: str
    risks: list[str]

@app.endpoint(route="/review")
def review_patch(diff: str) -> Review:
    return analyze(diff)`;

const functionExample = `from lazycloud import App

app = App("test_suite")

@app.function(cpu=4.0, memory="4Gi")
def run_test_shard(shard: int, total: int) -> dict:
    return run_tests(shard=shard, total=total)`;

const sandboxExample = `from lazycloud import App

app = App("coding_agent")
workspace = app.sandbox(
    name="workspace",
    block_network=True,
)

instance = workspace.create()
result = instance.run("pytest -q")
print(result.exit_code)`;

const podExample = `from lazycloud import App, Image

app = App("preview")
preview = app.pod(
    name="web",
    image=Image.from_dockerfile(
        "Dockerfile",
        context_dir=".",
    ),
    command=["npm", "run", "preview", "--", "--host", "0.0.0.0"],
    ports={"http": 4173},
)`;

const cronExample = `from lazycloud import App

app = App("maintenance")

@app.cron("0 2 * * *", retries=2)
def nightly_evals() -> dict:
    return evaluate_latest_release()`;

const heroStories = [
  {
    key: "apps",
    label: "Apps + APIs",
    code: endpointExample,
    command: "lazycloud deploy application.py:app",
    status: "deployed",
  },
  {
    key: "functions",
    label: "Functions",
    code: functionExample,
    command: "lazycloud deploy application.py:app",
    status: "deployed",
  },
  {
    key: "sandboxes",
    label: "Sandboxes",
    code: sandboxExample,
    command: "python application.py",
    status: "sandbox ready",
  },
  {
    key: "services",
    label: "Services",
    code: podExample,
    command: "lazycloud deploy application.py:app",
    status: "deployed",
  },
  {
    key: "schedules",
    label: "Schedules",
    code: cronExample,
    command: "lazycloud deploy application.py:app",
    status: "deployed",
  },
] as const;

type PlatformStory = {
  key: string;
  label: string;
  title: string;
  body: string;
  notes?: string[];
  visual: StoryVisual;
};

const platformStories: PlatformStory[] = [
  {
    key: "endpoints",
    label: "Applications + APIs",
    title: "Ship an API the minute it is written.",
    body: "APIs and model endpoints with explicit resources, autoscaling, and scale-to-zero. No load balancer, no image to push.",
    notes: [
      "Scales to zero when idle.",
      "Keep-warm holds capacity when you want it ready.",
    ],
    visual: "application",
  },
  {
    key: "graphs",
    label: "Jobs + pipelines",
    title: "Outgrow one machine without rewriting anything.",
    body: "Tests, evals, data processing, and GPU jobs as dependency-aware tasks with durable results and logs. The same functions you called locally an hour ago.",
    notes: [
      "Size CPU, GPU, and memory per Task.",
      "Mount shared volumes so later stages read earlier output.",
    ],
    visual: "jobs",
  },
  {
    key: "background",
    label: "Background work",
    title: "Keep the work running after the session ends.",
    body: "Queue and schedule work with retries, cancellation, and live logs. Nothing depends on a terminal staying open.",
    notes: [
      "Workers scale with queue depth, from zero.",
      "Failed runs retry on your policy.",
      "Every Task is a handle: wait, subscribe, cancel.",
    ],
    visual: "background",
  },
  {
    key: "sandboxes",
    label: "Agent sandboxes",
    title: "Give agents compute with boundaries.",
    body: "Isolated workspaces with files, processes, ports, Docker, snapshots, and explicit network policy.",
    notes: [
      "Run any process, stream its output.",
      "Block all egress, or pin an allow-list.",
      "Snapshot filesystem or memory, restore from it.",
    ],
    visual: "sandbox",
  },
];

const parityDefinition = `@app.function(gpu="A100-40", memory="16Gi")
def embed(batch: list[str]) -> list[list[float]]:
    return model.encode(batch)`;

const parityModes = [
  {
    key: "local",
    Plate: LocalPlate,
    stage: "01 · Develop",
    title: "Run it locally",
    call: "embed.local(rows)",
    body: "Call it in-process with your debugger, breakpoints, and local data.",
  },
  {
    key: "gpu",
    Plate: GpuPlate,
    stage: "02 · Run",
    title: "Run it in the cloud",
    call: "embed.remote(rows)",
    body: "Invoke it whenever you need a GPU or more capacity. No deployment required.",
  },
  {
    key: "production",
    Plate: ProductionPlate,
    stage: "03 · Deploy",
    title: "Deploy it as a service",
    call: "lazycloud deploy app.py:app",
    body: "Publish a stable endpoint that stays available while compute scales to zero between calls.",
  },
];

/* Placed after the control-plane rail: the rail establishes that one platform
   covers every phase, and this is the sharpest instance of it — the same
   function, unchanged, across all three execution modes. Ahead of the
   typed-client story, which is a narrower point. */
function ParitySection() {
  return (
    <section className="border-t border-input bg-background py-18 sm:py-22 lg:py-28">
      <div className={shell}>
        <SectionHeading
          label="Develop · run · deploy"
          title={
            <>
              One function. <em>From local to live.</em>
            </>
          }
          body="Call it in-process while you build. Run it in the cloud whenever you need a GPU or more capacity, without creating a deployment. When you need a stable endpoint, deploy the same function as a service. No rewrite between steps."
        />

        <div className="max-w-[820px]">
          <CodeBlock
            className="shadow-[8px_8px_0_0_var(--secondary)]"
            tone="paper"
            bodyClassName="p-4 text-[11.5px] leading-[1.75] sm:p-5 sm:text-[12.5px]"
          >
            {parityDefinition}
          </CodeBlock>
        </div>

        <div className="mt-10 grid gap-x-5 gap-y-8 sm:grid-cols-3">
          {parityModes.map((mode) => (
            <article key={mode.key}>
              <mode.Plate />
              <p className="mt-5 font-mono text-[10px] font-medium tracking-[0.12em] text-brand uppercase">
                {mode.stage}
              </p>
              <h3 className="mt-2 text-[19px] leading-tight font-medium sm:min-h-12">
                {mode.title}
              </h3>
              <code className="mt-2 block font-mono text-[12px] break-all text-brand">
                {mode.call}
              </code>
              <p className="mt-2 text-[13px] leading-relaxed text-muted-foreground">{mode.body}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function MarketingHome() {
  return (
    <MarketingLayout>
      <main id="marketing-main">
        <section className="relative min-h-[680px] overflow-hidden border-b border-border bg-background sm:min-h-[720px] lg:min-h-[760px]">
          <div className="marketing-grid-field" aria-hidden="true" />
          <div
            className={cn(
              shell,
              "relative z-[2] grid grid-cols-[0.84fr_1.16fr] items-center gap-10 pt-12 pb-16 sm:gap-12 sm:pt-16 sm:pb-20 lg:min-h-[700px] lg:gap-16 lg:pt-23 lg:pb-13 max-lg:grid-cols-1",
            )}
          >
            <div className="marketing-rise">
              <Pill>Code got fast. Infrastructure didn&apos;t.</Pill>
              <h1 className="max-w-[620px] text-balance font-serif text-[clamp(42px,8vw,88px)] leading-[0.96] font-normal tracking-[-0.005em] sm:mt-3 lg:mt-6 lg:text-[clamp(52px,6.4vw,88px)] [&_em]:text-brand [&_em]:italic">
                Deploy as fast as you <em>develop.</em>
              </h1>
              <p className="mt-5 max-w-[540px] text-base leading-[1.58] text-muted-foreground sm:mt-6 sm:text-lg">
                Code arrives in seconds, then waits on Dockerfiles, registries, load balancers,
                IAM. One decorator makes a Python function a deployed API, job, queue, or GPU
                workload. It still runs on your laptop.
              </p>
              <div className="mt-7 flex flex-col gap-2.5 sm:flex-row sm:flex-wrap">
                <GetStartedButton className="marketing-action-primary stamp border-brand/45" />
                {EXAMPLES_URL ? (
                  <Button
                    asChild
                    size="lg"
                    variant="outline"
                    className="marketing-button-link marketing-action-secondary stamp-quiet justify-between border-input text-foreground [@media(pointer:coarse)]:min-h-11 max-[479px]:w-full"
                  >
                    <a href={EXAMPLES_URL}>
                      <span>Explore examples</span>
                      <Glyph>↗</Glyph>
                    </a>
                  </Button>
                ) : null}
              </div>
            </div>

            <Tabs
              className="marketing-rise relative z-[2] min-w-0 rounded-2xl border border-input bg-card text-foreground shadow-[8px_8px_0_0_var(--secondary)] [animation-delay:100ms]"
              defaultValue={heroStories[0].key}
            >
              <TabsList
                /* The split hero keeps a stable 3×2 control through compact
                   desktop widths; only the full-width canvas uses one row. */
                className="grid h-auto w-full grid-cols-3 gap-1 p-2 xl:flex xl:min-h-12.5 xl:flex-wrap xl:justify-start"
                aria-label="Hero code examples"
              >
                {heroStories.map((story) => (
                  <TabsTrigger
                    className="h-11 min-w-0 px-1 text-[11px] sm:text-xs xl:w-auto xl:shrink-0 xl:px-3"
                    key={story.key}
                    value={story.key}
                  >
                    {story.label}
                  </TabsTrigger>
                ))}
              </TabsList>
              {heroStories.map((story) => (
                <TabsContent key={story.key} value={story.key}>
                  <CodeBlock
                    className="rounded-none border-0 bg-transparent"
                    tone="paper"
                    /* Fixed body height so switching examples never resizes the
                       panel; sized to the tallest snippet. On narrow screens the
                       type eases down a notch so wrapped lines still fit without
                       a scroll. */
                    bodyClassName="h-[300px] p-4 text-[11px] leading-[1.7] max-[359px]:h-[264px] max-[359px]:p-3 max-[359px]:text-[10px] max-[359px]:leading-[1.6] sm:h-[340px] sm:p-6 sm:text-[11.5px] sm:leading-[1.75]"
                    footer={
                      <div
                        className="flex min-h-11 min-w-0 flex-wrap items-center gap-x-2 gap-y-1 py-2"
                        data-marketing-terminal-surface=""
                      >
                        <span className="text-brand">$</span>
                        <span className="min-w-0 flex-1 break-words [overflow-wrap:anywhere]">
                          {story.command}
                        </span>
                        <i className="marketing-cursor" aria-hidden="true" />
                        <strong className="ml-auto inline-flex shrink-0 items-center gap-1.5 font-medium text-positive">
                          <StatusDot /> {story.status}
                        </strong>
                      </div>
                    }
                  >
                    {story.code}
                  </CodeBlock>
                </TabsContent>
              ))}
            </Tabs>
          </div>
        </section>

        <PlatformStoryRail />

        <ParitySection />

        <StubsSection />

        <ComputeSection />

        <section className="border-t border-input bg-muted py-18 sm:py-22 lg:py-28">
          <div className={shell}>
            <div className="flex flex-col items-start gap-0 sm:flex-row sm:items-end sm:justify-between sm:gap-10">
              <SectionHeading title="What will you build?" />
              {EXAMPLES_URL ? (
                <div className="-mt-6 mb-10 sm:mt-0 sm:mb-14">
                  <a
                    className="inline-flex min-h-11 items-center gap-2.5 text-[13px] font-semibold text-foreground"
                    href={EXAMPLES_URL}
                  >
                    Explore examples
                    <Glyph>→</Glyph>
                  </a>
                </div>
              ) : null}
            </div>
            <div
              className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5"
              role="region"
              tabIndex={0}
              aria-label="Runnable examples"
            >
              {marketingUseCases.map((useCase) => (
                /* A card is a link only when there is a gallery to open. Without
                   one it still says what you can build, but it stops offering a
                   press that goes nowhere — and the arrow goes with it, since the
                   arrow is the promise. */
                <UseCaseCard key={useCase.id}>
                  <MarketingExampleImage
                    className="absolute inset-x-0 top-0 h-[56%] object-cover object-[center_72%] saturate-[0.8] contrast-[0.92] brightness-[1.04] blur-[0.2px]"
                    src={useCase.imageSrc}
                  />
                  <div
                    className="pointer-events-none absolute inset-0 bg-[linear-gradient(180deg,transparent_0%,color-mix(in_oklab,var(--card)_8%,transparent)_32%,color-mix(in_oklab,var(--card)_80%,transparent)_50%,var(--card)_59%)]"
                    aria-hidden="true"
                  />
                  <div
                    className="absolute inset-x-0 top-[46%] bottom-0 flex flex-col p-4 text-foreground xl:p-5"
                    data-marketing-example-copy
                  >
                    <h3 className="max-w-[390px] text-[clamp(1.1rem,1.55vw,1.4rem)] leading-[1.12] font-medium">
                      {useCase.title}
                    </h3>
                    <p className="mt-3 max-w-[390px] text-[12px] leading-[1.5] text-muted-foreground">
                      {useCase.cardSummary}
                    </p>
                    {EXAMPLES_URL ? (
                      <span className="mt-auto inline-flex text-muted-foreground/55">
                        <Glyph>↗</Glyph>
                      </span>
                    ) : null}
                  </div>
                </UseCaseCard>
              ))}
            </div>
          </div>
        </section>

        <FinalCta
          title={
            <>
              Your cloud should <em>accelerate you</em>, not be the bottleneck.
            </>
          }
          body="Write the function. Call it locally. Run it on a GPU when it needs one. Apps, jobs, queues, schedules, sandboxes — our compute or yours."
        />
      </main>
    </MarketingLayout>
  );
}

const useCaseCard =
  "relative block aspect-[3/4] w-full overflow-hidden rounded-xl border border-border bg-card text-left text-foreground sm:last:col-span-2 xl:last:col-span-1";

function UseCaseCard({ children }: { children: ReactNode }) {
  if (!EXAMPLES_URL) {
    return <div className={useCaseCard}>{children}</div>;
  }
  return (
    <a className={useCaseCard} href={EXAMPLES_URL}>
      {children}
    </a>
  );
}

/* The marketing shell scrolls inside its own element, so the story
   controller measures against that scrollport instead of assuming the window. */
function findScrollport(node: HTMLElement): HTMLElement | null {
  for (let parent = node.parentElement; parent; parent = parent.parentElement) {
    const overflowY = window.getComputedStyle(parent).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return parent;
  }
  return null;
}

function usePlatformStoryScroll(onScrollSelect: (index: number) => void) {
  const sectionRef = useRef<HTMLElement>(null);
  const storyRefs = useRef<Array<HTMLElement | null>>([]);
  const [scrollDriven, setScrollDriven] = useState(false);

  const registerStory = useCallback((index: number, node: HTMLElement | null) => {
    storyRefs.current[index] = node;
  }, []);

  useEffect(() => {
    const query = window.matchMedia("(min-width: 1024px)");
    const sync = () => setScrollDriven(query.matches);
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;

    const scrollport = findScrollport(section);
    const scrollTarget: HTMLElement | Window = scrollport ?? window;
    let frame = 0;

    const measure = () => {
      frame = 0;
      const scrollportRect = scrollport?.getBoundingClientRect();
      const viewportTop = scrollportRect?.top ?? 0;
      const viewportHeight = scrollport?.clientHeight ?? window.innerHeight;
      const header = section.ownerDocument.querySelector<HTMLElement>(".marketing-site > header");
      const headerHeight = header?.offsetHeight ?? 0;
      const readingLine =
        viewportTop +
        headerHeight +
        Math.min(120, Math.max(48, (viewportHeight - headerHeight) * 0.2));

      let activeIndex = 0;
      for (const [index, story] of storyRefs.current.entries()) {
        if (story && story.getBoundingClientRect().top <= readingLine) {
          activeIndex = index;
        }
      }
      onScrollSelect(activeIndex);
    };

    const schedule = () => {
      if (frame === 0) frame = window.requestAnimationFrame(measure);
    };

    measure();
    scrollTarget.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      if (frame !== 0) window.cancelAnimationFrame(frame);
      scrollTarget.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, [onScrollSelect, scrollDriven]);

  const moveToStory = useCallback(
    (index: number) => {
      const section = sectionRef.current;
      const story = storyRefs.current[index];
      if (!section || !story) return;

      const scrollport = findScrollport(section);
      const scrollportRect = scrollport?.getBoundingClientRect();
      const viewportTop = scrollportRect?.top ?? 0;
      const scrollPosition = scrollport ? scrollport.scrollTop : window.scrollY;
      const header = section.ownerDocument.querySelector<HTMLElement>(".marketing-site > header");
      const headerHeight = header?.offsetHeight ?? 0;
      const target =
        scrollPosition + story.getBoundingClientRect().top - viewportTop - headerHeight - 32;
      const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth";

      onScrollSelect(index);
      (scrollport ?? window).scrollTo({ top: target, behavior });
    },
    [onScrollSelect],
  );

  return { sectionRef, registerStory, moveToStory, scrollDriven };
}

function PlatformStoryRail() {
  const [activeKey, setActiveKey] = useState(platformStories[0].key);
  const selectByIndex = useCallback((index: number) => {
    setActiveKey(platformStories[index]?.key ?? platformStories[0].key);
  }, []);
  const { sectionRef, registerStory, moveToStory, scrollDriven } =
    usePlatformStoryScroll(selectByIndex);

  return (
    <section
      id="platform"
      className="relative border-t border-input bg-muted"
      data-scroll-driven={scrollDriven ? "true" : "false"}
      ref={sectionRef}
    >
      <div
        className={cn(
          shell,
          "grid items-start gap-12 py-18 sm:gap-16 sm:py-22 lg:grid-cols-[minmax(270px,0.72fr)_minmax(0,1.28fr)] lg:gap-16 lg:py-0",
        )}
      >
        <aside className="min-w-0 lg:sticky lg:top-28 lg:self-start lg:py-16">
          <SectionHeading
            label="SDK · CLI --json · typed clients"
            title={
              <>
                Every phase. <em>One control plane.</em>
              </>
            }
            body="Build, test, deploy, scale, operate — usually five tools and five afternoons. Here it is one SDK, one JSON CLI, and typed clients. Developers and agents reach for the same thing."
          />
          <nav aria-label="Platform use cases" className="border-t border-border">
            <ol className="m-0 list-none p-0">
              {platformStories.map((story, index) => {
                const active = story.key === activeKey;
                return (
                  <li key={story.key}>
                    <button
                      aria-controls={`platform-story-${story.key}`}
                      aria-current={active ? "location" : undefined}
                      className={cn(
                        "flex min-h-12 w-full items-center gap-3 border-b border-border bg-transparent py-3 pr-1 pl-3 text-left text-sm text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:min-h-14 sm:pl-4",
                        active && "font-medium text-foreground shadow-[inset_3px_0_0_var(--brand)]",
                      )}
                      onClick={() => moveToStory(index)}
                      type="button"
                    >
                      <span className="mr-4 font-mono text-[15px] text-brand">
                        {String(index + 1).padStart(2, "0")}
                      </span>
                      {story.label}
                    </button>
                  </li>
                );
              })}
            </ol>
          </nav>
        </aside>

        <div className="min-w-0">
          {platformStories.map((story, index) => (
            <article
              aria-labelledby={`platform-story-${story.key}-title`}
              className="scroll-mt-28 border-b border-border py-10 first:pt-0 last:border-b-0 last:pb-0 sm:py-12 lg:py-10 lg:first:pt-16 lg:last:pb-16"
              data-platform-story={story.key}
              id={`platform-story-${story.key}`}
              key={story.key}
              ref={(node) => registerStory(index, node)}
            >
              <div className="mb-6 sm:mb-8">
                <p className="font-mono text-[11px] text-brand">
                  {String(index + 1).padStart(2, "0")} / {story.label}
                </p>
                <h3
                  className="mt-3 text-2xl leading-tight font-medium sm:text-3xl"
                  id={`platform-story-${story.key}-title`}
                >
                  {story.title}
                </h3>
                <p className="mt-3 max-w-[620px] text-sm leading-relaxed text-muted-foreground sm:text-base">
                  {story.body}
                </p>
                <div className="mt-4 grid gap-2 sm:grid-cols-2">
                  {story.notes?.map((note) => (
                    <p
                      className="flex items-start gap-2 font-mono text-[10.5px] leading-relaxed text-brand"
                      key={note}
                    >
                      <Glyph>↳</Glyph>
                      <span>{note}</span>
                    </p>
                  ))}
                </div>
              </div>
              <div
                aria-label={`${story.label} preview`}
                className="marketing-story-panel min-w-0"
                role="region"
              >
                <div className="marketing-story-visual flex h-[390px] sm:h-[430px] lg:h-[clamp(380px,46dvh,430px)] [&>div]:flex-1">
                  <StoryPreview visual={story.visual} />
                </div>
              </div>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

const computePaths = [
  {
    icon: "◎",
    title: "Managed serverless",
    body: "Managed CPU capacity, no setup. Scales down when idle.",
    command: null,
  },
  {
    icon: "⇄",
    title: "Connected AWS",
    body: "Connect an AWS account. Place CPU or GPU workloads in it.",
    command: "lazycloud cloud connect aws",
  },
  {
    icon: ">_",
    title: "Any machine",
    body: "Join a Linux VM, bare-metal box, or on-prem GPU. No definition changes.",
    command: "lazycloud machine join",
  },
];

function ComputeSection() {
  return (
    <section className="border-t border-input bg-background py-18 sm:py-22 lg:py-28">
      <div className={shell}>
        <SectionHeading
          title={
            <>
              One workload model. <em>Managed compute or yours.</em>
            </>
          }
          body="Where it runs is a setting, not a rewrite. Managed capacity, your own AWS account, or a machine under your desk — same definitions, no code changes."
        />
        <div className="grid grid-cols-[0.9fr_1.1fr] gap-7 max-lg:grid-cols-1">
          <div className="flex flex-col gap-4">
            {computePaths.map((path) => (
              <article
                className="flex-1 rounded-2xl border border-border bg-card p-5 sm:p-6"
                key={path.title}
              >
                <div className="flex items-center gap-3">
                  <span className="inline-flex size-9 items-center justify-center rounded-xl border border-brand/25 text-brand">
                    <Glyph>{path.icon}</Glyph>
                  </span>
                  <h3 className="text-lg font-medium tracking-[-0.02em]">{path.title}</h3>
                </div>
                <p className="mt-3 text-[13px] text-muted-foreground">{path.body}</p>
                {path.command ? (
                  <code className="mt-3 inline-flex max-w-full items-center gap-2 rounded-md border border-border bg-muted/60 px-2.5 py-1.5 font-mono text-[11px] break-all text-foreground">
                    <span className="text-brand">$</span> {path.command}
                  </code>
                ) : (
                  <p className="mt-3 inline-flex items-center gap-2 font-mono text-[11px] text-brand">
                    <Glyph>↳</Glyph>
                    Default placement, no setup
                  </p>
                )}
              </article>
            ))}
          </div>
          <div className="flex [&>div]:flex-1">
            <ComputePlacementPreview />
          </div>
        </div>
      </div>
    </section>
  );
}

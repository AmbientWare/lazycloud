import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowUpRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EXAMPLES_URL } from "@/lib/env";
import { cn } from "@/lib/utils";

import { MarketingLayout } from "./MarketingLayout";
import { MarketingReveal } from "./MarketingReveal";
import { StubsSection } from "./StubsSection";
import {
  FinalCta,
  GetStartedButton,
  MarketingCard,
  MarketingHero,
  SectionHeading,
  StatusDot,
  shell,
} from "./MarketingPrimitives";
import { ComputePlacementPreview, StoryPreview, type StoryVisual } from "./ProductPreviews";
import { MarketingExampleImage } from "./MarketingExampleImage";
import { marketingUseCases } from "./marketingUseCases";
import { GpuPlate, LocalPlate, ProductionPlate } from "./ParityFigures";

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

@app.function(cron="0 2 * * *", retries=2)
def nightly_evals() -> dict:
    return evaluate_latest_release()`;

const heroStories = [
  {
    key: "apps",
    label: "APIs",
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
  visual: StoryVisual;
};

const platformStories: PlatformStory[] = [
  {
    key: "endpoints",
    label: "APIs",
    title: "Ship an endpoint or a full ASGI app.",
    body: "Deploy a Python function as an API, or bring your ASGI app with its routes and middleware. LazyCloud handles TLS, scaling, and idle shutdown.",
    visual: "application",
  },
  {
    key: "graphs",
    label: "Jobs and pipelines",
    title: "Run the work behind your app.",
    body: "Send tests, evals, data processing, and GPU jobs to the cloud. Connect tasks with dependencies and follow their logs and saved results.",
    visual: "jobs",
  },
  {
    key: "background",
    label: "Background jobs and crons",
    title: "Keep working after the request ends.",
    body: "Submit background tasks or add a cron schedule to a function. Each run has retries, cancellation, and live logs.",
    visual: "background",
  },
  {
    key: "sandboxes",
    label: "Agent sandboxes",
    title: "Give your coding agent room to run.",
    body: "Let your agent run code and tests in an isolated sandbox with files, processes, ports, Docker, snapshots, and a network policy you control.",
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
    title: "Debug locally",
    call: "embed.local(rows)",
    body: "Use local data and your usual debugger.",
  },
  {
    key: "gpu",
    Plate: GpuPlate,
    title: "Run once in the cloud",
    call: "embed.remote(rows)",
    body: "Send a run to cloud compute and get the result. No deployment required.",
  },
  {
    key: "production",
    Plate: ProductionPlate,
    title: "Deploy your app",
    call: "lazycloud deploy app.py:app",
    body: "Publish the function so your services can call it.",
  },
];

function ParitySection() {
  return (
    <section className="marketing-parity border-t border-input bg-background-subtle py-18 sm:py-22 lg:py-28">
      <div className={shell}>
        <SectionHeading
          title={
            <>
              One function. <em>Three ways to run it.</em>
            </>
          }
          body="Debug on your laptop, send a one-off cloud run, or deploy the app. The function stays the same."
        />

        <MarketingReveal className="relative max-w-[820px]" delay={80}>
          <MarketingCard asChild>
            <CodeBlock
              tone="paper"
              bodyClassName="p-4 text-[11.5px] leading-[1.75] sm:p-5 sm:text-[12.5px]"
            >
              {parityDefinition}
            </CodeBlock>
          </MarketingCard>
        </MarketingReveal>

        <div className="mt-10 grid gap-x-5 gap-y-8 sm:grid-cols-3">
          {parityModes.map((mode, index) => (
            <MarketingReveal className="relative" key={mode.key} delay={index * 70}>
              <article>
                <mode.Plate />
                <h3 className="mt-5 text-[19px] leading-tight font-medium sm:min-h-12">
                  {mode.title}
                </h3>
                <code className="mt-2 block font-mono text-[12px] break-all text-brand">
                  {mode.call}
                </code>
                <p className="mt-2 text-[13px] leading-relaxed text-muted-foreground">
                  {mode.body}
                </p>
              </article>
            </MarketingReveal>
          ))}
        </div>
      </div>
    </section>
  );
}

export function MarketingHome() {
  return (
    <MarketingLayout>
      <main className="marketing-hero-page" id="marketing-main">
        <MarketingHero>
          <div className="relative">
            <h1 className="max-w-[620px] text-balance">
              Deploy as fast <em>as you develop.</em>
            </h1>
            <p className="mt-5 max-w-[540px] text-base leading-[1.58] text-muted-foreground sm:mt-6 sm:text-lg">
              Your coding agent helps you build faster. LazyCloud gets your product running. Use the
              same Python code locally, for a one-off cloud run, or as a deployed app.
            </p>
            <div className="mt-7 flex flex-col gap-2.5 sm:flex-row sm:flex-wrap">
              <GetStartedButton />
              <Button
                asChild
                size="lg"
                variant="secondary"
                className="marketing-button-link justify-between [@media(pointer:coarse)]:min-h-11 max-[479px]:w-full"
              >
                <a href={`${EXAMPLES_URL}/index`}>
                  <span>Explore examples</span>
                  <ArrowUpRight aria-hidden="true" />
                </a>
              </Button>
            </div>
          </div>

          <div className="relative min-w-0">
            <MarketingCard className="relative z-[2] min-w-0">
              <Tabs className="min-w-0 text-foreground" defaultValue={heroStories[0].key}>
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
            </MarketingCard>
          </div>
        </MarketingHero>

        <ParitySection />

        <PlatformStoryRail />

        <StubsSection />

        <ComputeSection />

        <section className="border-t border-input bg-background-subtle py-18 sm:py-22 lg:py-28">
          <div className={shell}>
            <div className="flex flex-col items-start gap-0 sm:flex-row sm:items-end sm:justify-between sm:gap-10">
              <SectionHeading title="Example projects" />
              <div className="-mt-6 mb-10 sm:mt-0 sm:mb-14">
                <a
                  className="inline-flex min-h-11 items-center gap-2.5 text-[13px] font-semibold text-foreground"
                  href={`${EXAMPLES_URL}/index`}
                >
                  Explore examples
                  <ArrowUpRight className="size-4.5 shrink-0" aria-hidden="true" />
                </a>
              </div>
            </div>
            <MarketingReveal
              delay={80}
              className="relative grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5"
              role="region"
              tabIndex={0}
              aria-label="Runnable examples"
            >
              {marketingUseCases.map((useCase) => (
                <UseCaseCard key={useCase.id} href={`${EXAMPLES_URL}/${useCase.id}`}>
                  <MarketingExampleImage
                    className="absolute inset-x-0 top-0 h-[56%] object-cover object-[center_72%]"
                    src={useCase.imageSrc}
                  />
                  <div
                    className="pointer-events-none absolute inset-0 bg-[linear-gradient(180deg,transparent_0%,color-mix(in_oklab,var(--card)_8%,transparent)_32%,color-mix(in_oklab,var(--card)_80%,transparent)_50%,var(--card)_59%)]"
                    aria-hidden="true"
                  />
                  <div
                    className="relative mt-[61%] flex flex-1 flex-col p-4 text-foreground xl:p-5"
                    data-marketing-example-copy
                  >
                    <h3 className="max-w-[390px] text-[clamp(1.1rem,1.55vw,1.4rem)] leading-[1.12] font-medium">
                      {useCase.title}
                    </h3>
                    <p className="mt-3 max-w-[390px] text-[12px] leading-[1.5] text-muted-foreground">
                      {useCase.cardSummary}
                    </p>
                    <span className="mt-auto inline-flex pt-3 text-brand">
                      <ArrowUpRight className="size-4.5 shrink-0" aria-hidden="true" />
                    </span>
                  </div>
                </UseCaseCard>
              ))}
            </MarketingReveal>
          </div>
        </section>

        <FinalCta
          title={
            <>
              Deploy your <em>first workload.</em>
            </>
          }
          body="Take the code you built locally and put it to work in the cloud. Deploy an API, launch a background job, or schedule its next run."
        />
      </main>
    </MarketingLayout>
  );
}

const useCaseCard =
  "relative flex min-h-80 w-full flex-col text-left text-foreground sm:last:col-span-2 xl:last:col-span-1";

function UseCaseCard({ children, href }: { children: ReactNode; href: string }) {
  return (
    <MarketingCard asChild>
      <a className={useCaseCard} href={href}>
        {children}
      </a>
    </MarketingCard>
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
      className="relative border-t border-input bg-background"
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
            title={
              <>
                Ship the <em>whole product.</em>
              </>
            }
            body="Cloud functions, HTTP endpoints, full ASGI apps, background jobs, and cron jobs. Define them in Python alongside your code."
          />
          <nav aria-label="Platform use cases" className="relative border-t border-border">
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
                      {story.label}
                    </button>
                  </li>
                );
              })}
            </ol>
          </nav>
        </aside>

        <MarketingReveal className="min-w-0">
          {platformStories.map((story, index) => (
            <article
              aria-labelledby={`platform-story-${story.key}-title`}
              className="scroll-mt-28 border-b border-border py-10 first:pt-0 last:border-b-0 last:pb-0 sm:py-12 lg:py-10 lg:first:pt-16 lg:last:pb-16"
              data-platform-story={story.key}
              id={`platform-story-${story.key}`}
              key={story.key}
              ref={(node) => registerStory(index, node)}
            >
              <div className="relative mb-6 sm:mb-8">
                <p className="text-sm font-medium text-brand">{story.label}</p>
                <h3
                  className="mt-3 text-2xl leading-tight font-medium sm:text-3xl"
                  id={`platform-story-${story.key}-title`}
                >
                  {story.title}
                </h3>
                <p className="mt-3 max-w-[620px] text-sm leading-relaxed text-muted-foreground sm:text-base">
                  {story.body}
                </p>
              </div>
              <div
                aria-label={`${story.label} preview`}
                className="relative marketing-story-panel min-w-0"
                role="region"
              >
                <div className="marketing-story-visual flex h-[390px] sm:h-[430px] lg:h-[clamp(380px,46dvh,430px)] [&>div]:flex-1">
                  <StoryPreview visual={story.visual} />
                </div>
              </div>
            </article>
          ))}
        </MarketingReveal>
      </div>
    </section>
  );
}

const computePaths = [
  {
    title: "LazyCloud",
    body: "Run CPU workloads without managing servers. Capacity scales down when idle.",
  },
  {
    title: "Your AWS account",
    body: "Run CPU and GPU workloads in your own AWS account.",
  },
  {
    title: "Your own machines",
    body: "Connect supported Linux servers, VMs, or GPU machines you already own.",
  },
];

function ComputeSection() {
  return (
    <section id="compute" className="border-t border-input bg-background py-18 sm:py-22 lg:py-28">
      <div
        className={cn(
          shell,
          "grid items-center gap-10 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)] lg:gap-16",
        )}
      >
        <div className="min-w-0">
          <SectionHeading
            title={
              <>
                Choose where <em>your code runs.</em>
              </>
            }
            body="Start on LazyCloud. Connect AWS or your own Linux machines when you need control of the infrastructure."
          />
          <MarketingReveal className="border-t border-border" delay={80}>
            {computePaths.map((path) => (
              <article className="border-b border-border py-5" key={path.title}>
                <h3 className="text-lg font-medium tracking-[-0.02em]">{path.title}</h3>
                <p className="mt-2 max-w-[46ch] text-sm leading-relaxed text-muted-foreground">
                  {path.body}
                </p>
              </article>
            ))}
          </MarketingReveal>
        </div>
        <MarketingReveal className="flex min-w-0 [&>div]:flex-1" delay={140}>
          <ComputePlacementPreview />
        </MarketingReveal>
      </div>
    </section>
  );
}

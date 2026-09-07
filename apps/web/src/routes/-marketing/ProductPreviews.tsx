import type { ReactNode } from "react";

import { CodeBlock } from "@/components/ui/code-block";

import { MarketingCard } from "./MarketingPrimitives";

export type StoryVisual = "application" | "jobs" | "background" | "sandbox";

function PreviewFrame({ title, children }: { title: string; children: ReactNode }) {
  return (
    <MarketingCard className="marketing-product-frame" data-product-preview="">
      <div className="marketing-product-bar">
        <strong>{title}</strong>
      </div>
      {children}
    </MarketingCard>
  );
}

function PreviewChart({ label, path }: { label: string; path: string }) {
  return (
    <figure className="marketing-preview-chart">
      <figcaption className="sr-only">{label}</figcaption>
      <svg viewBox="0 0 480 160" preserveAspectRatio="none" aria-hidden="true">
        {[30, 80, 130].map((y) => (
          <line className="marketing-preview-grid" key={y} x1="0" x2="480" y1={y} y2={y} />
        ))}
        <path className="marketing-preview-trace" d={path} />
      </svg>
      <div className="marketing-preview-axis">
        <span>60 seconds ago</span>
        <span>Now</span>
      </div>
    </figure>
  );
}

const pipelineTasks = [
  { name: "checkout", start: 0, duration: 4 },
  { name: "test", start: 4, duration: 6 },
  { name: "evaluate", start: 10, duration: 12 },
  { name: "security_scan", start: 22, duration: 5 },
  { name: "publish", start: 27, duration: 3 },
];

const sandboxSession = `$ pytest -q tests/test_auth.py
1 failed, 24 passed in 3.2s

$ grep -rn "token_ttl" src/
src/auth.py:41: token_ttl = 0

# The agent updates src/auth.py.
$ pytest -q tests/test_auth.py
25 passed in 2.9s`;

export function StoryPreview({ visual }: { visual: StoryVisual }) {
  if (visual === "application") {
    return (
      <PreviewFrame title="Example API traffic">
        <PreviewChart
          label="Request load rises and settles as containers serve traffic."
          path="M0 120 H30 V108 H60 V115 H90 V85 H120 V92 H150 V58 H180 V65 H210 V38 H240 V54 H270 V45 H300 V75 H330 V68 H360 V90 H390 V82 H420 V108 H450 V96 H480"
        />
      </PreviewFrame>
    );
  }
  if (visual === "jobs") {
    return (
      <PreviewFrame title="Example pipeline">
        <ol
          className="marketing-preview-timeline"
          aria-label="Example pipeline, each task starts after its dependency completes. Total time 30 seconds."
        >
          {pipelineTasks.map((task) => (
            <li key={task.name}>
              <span>{task.name}</span>
              <div className="marketing-preview-track" aria-hidden="true">
                <i
                  style={{
                    marginLeft: `${(task.start / 30) * 100}%`,
                    width: `${(task.duration / 30) * 100}%`,
                  }}
                />
              </div>
              <span>{task.duration}s</span>
            </li>
          ))}
        </ol>
      </PreviewFrame>
    );
  }
  if (visual === "background") {
    return (
      <PreviewFrame title="Example queue backlog">
        <PreviewChart
          label="The queue drains as workers finish the backlog."
          path="M0 115 L30 102 L60 65 L90 40 L120 32 L150 44 L180 57 L210 73 L240 86 L270 98 L300 112 L330 124 L360 136 L390 145 L420 145 L450 145 L480 145"
        />
      </PreviewFrame>
    );
  }
  return (
    <PreviewFrame title="Example agent session">
      <CodeBlock
        className="rounded-none border-0 shadow-none"
        tone="paper"
        bodyClassName="p-5 text-xs leading-relaxed sm:text-[13px]"
      >
        {sandboxSession}
      </CodeBlock>
    </PreviewFrame>
  );
}

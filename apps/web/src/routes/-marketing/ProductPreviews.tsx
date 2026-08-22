import type { ReactNode } from "react";

import { LiveComputePreview } from "./LiveComputePreview";
import { LiveEndpointChart } from "./LiveEndpointChart";
import { LiveQueuePreview } from "./LiveQueuePreview";
import { LiveSandboxPreview } from "./LiveSandboxPreview";
import { LiveTaskTimeline } from "./LiveTaskTimeline";

export type StoryVisual = "application" | "jobs" | "background" | "sandbox";

function ProductFrame({
  title,
  detail,
  status = "live",
  children,
  className = "",
}: {
  title: string;
  detail: string;
  status?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`marketing-product-frame ${className}`.trim()} data-product-preview="">
      <div className="marketing-product-bar">
        <div>
          <strong>{title}</strong>
          <span>{detail}</span>
        </div>
        <code>
          <i /> {status}
        </code>
      </div>
      {children}
    </div>
  );
}

export function StoryPreview({ visual }: { visual: StoryVisual }) {
  if (visual === "application") return <ApplicationPreview />;
  if (visual === "jobs") return <TaskGraphPreview />;
  if (visual === "background") return <QueuePreview />;
  return <SandboxPreview />;
}

function TaskGraphPreview() {
  return (
    <ProductFrame title="Release pipeline" detail="release-evals · production">
      <LiveTaskTimeline />
    </ProductFrame>
  );
}

function ApplicationPreview() {
  return (
    <ProductFrame title="Application traffic" detail="review-api · autoscaling">
      <LiveEndpointChart />
    </ProductFrame>
  );
}

function QueuePreview() {
  return (
    <ProductFrame title="Build queue" detail="release-checks · live">
      <LiveQueuePreview />
    </ProductFrame>
  );
}

function SandboxPreview() {
  return (
    <ProductFrame title="Agent workspace" detail="coding-agent · live">
      <LiveSandboxPreview />
    </ProductFrame>
  );
}

/* The homepage compute section's proof panel. */
export function ComputePlacementPreview() {
  return (
    <ProductFrame title="Placement map" detail="workspace capacity · live">
      <LiveComputePreview />
    </ProductFrame>
  );
}

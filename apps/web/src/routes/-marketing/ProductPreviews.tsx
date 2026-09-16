import type { ReactNode } from "react";

import { LiveComputePreview } from "./LiveComputePreview";
import { LiveEndpointChart } from "./LiveEndpointChart";
import { LiveQueuePreview } from "./LiveQueuePreview";
import { LiveSandboxPreview } from "./LiveSandboxPreview";
import { LiveTaskTimeline } from "./LiveTaskTimeline";
import { MarketingCard } from "./MarketingPrimitives";

export type StoryVisual = "application" | "jobs" | "background" | "sandbox";

function ProductFrame({
  title,
  detail,
  children,
}: {
  title: string;
  detail: string;
  children: ReactNode;
}) {
  return (
    <MarketingCard className="marketing-product-frame" data-product-preview="">
      <div className="marketing-product-bar">
        <div>
          <strong>{title}</strong>
          <span>{detail}</span>
        </div>
        <code>
          <i /> live
        </code>
      </div>
      {children}
    </MarketingCard>
  );
}

export function StoryPreview({ visual }: { visual: StoryVisual }) {
  if (visual === "application") return <LiveEndpointChart />;
  if (visual === "jobs") return <LiveTaskTimeline />;
  if (visual === "background") return <LiveQueuePreview />;
  return <LiveSandboxPreview />;
}

/* The homepage compute section's proof panel. */
export function ComputePlacementPreview() {
  return (
    <ProductFrame title="Compute" detail="Example workloads">
      <LiveComputePreview />
    </ProductFrame>
  );
}

import { LiveEndpointChart } from "./LiveEndpointChart";
import { LiveQueuePreview } from "./LiveQueuePreview";
import { LiveSandboxPreview } from "./LiveSandboxPreview";
import { LiveTaskTimeline } from "./LiveTaskTimeline";
import { PodPreview } from "./PodPreview";

export type StoryVisual = "application" | "jobs" | "background" | "sandbox" | "pod";

export function StoryPreview({ visual }: { visual: StoryVisual }) {
  if (visual === "application") return <LiveEndpointChart />;
  if (visual === "jobs") return <LiveTaskTimeline />;
  if (visual === "background") return <LiveQueuePreview />;
  if (visual === "pod") return <PodPreview />;
  return <LiveSandboxPreview />;
}

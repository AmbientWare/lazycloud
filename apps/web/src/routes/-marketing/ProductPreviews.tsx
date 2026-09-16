import { LiveEndpointChart } from "./LiveEndpointChart";
import { LiveQueuePreview } from "./LiveQueuePreview";
import { LiveSandboxPreview } from "./LiveSandboxPreview";
import { LiveTaskTimeline } from "./LiveTaskTimeline";

export type StoryVisual = "application" | "jobs" | "background" | "sandbox";

export function StoryPreview({ visual }: { visual: StoryVisual }) {
  if (visual === "application") return <LiveEndpointChart />;
  if (visual === "jobs") return <LiveTaskTimeline />;
  if (visual === "background") return <LiveQueuePreview />;
  return <LiveSandboxPreview />;
}

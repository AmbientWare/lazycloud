import { createLazyFileRoute } from "@tanstack/react-router";

import { MarketingHome } from "./-marketing/MarketingHome";

export const Route = createLazyFileRoute("/5")({
  component: () => <MarketingHome design="chapters" />,
});

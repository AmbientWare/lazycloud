import { createLazyFileRoute } from "@tanstack/react-router";

import { MarketingHome } from "./-marketing/MarketingHome";

export const Route = createLazyFileRoute("/1")({
  component: () => <MarketingHome design="quiet" />,
});

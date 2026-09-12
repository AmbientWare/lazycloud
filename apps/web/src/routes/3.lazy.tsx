import { createLazyFileRoute } from "@tanstack/react-router";

import { MarketingHome } from "./-marketing/MarketingHome";

export const Route = createLazyFileRoute("/3")({
  component: () => <MarketingHome design="light" />,
});

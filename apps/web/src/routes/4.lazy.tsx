import { createLazyFileRoute } from "@tanstack/react-router";

import { MarketingHome } from "./-marketing/MarketingHome";

export const Route = createLazyFileRoute("/4")({
  component: () => <MarketingHome design="gallery" />,
});

import { createLazyFileRoute } from "@tanstack/react-router";

import { MarketingHome } from "./-marketing/MarketingHome";

export const Route = createLazyFileRoute("/")({
  component: MarketingHome,
});

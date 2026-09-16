import { createLazyFileRoute } from "@tanstack/react-router";
import { MarketingHome } from "./-marketing/MarketingHome";
import { AircraftHero } from "./-marketing/AircraftHero";
export const Route = createLazyFileRoute("/1")({
  component: () => <MarketingHome hero={<AircraftHero />} />,
});

import { FlightHero } from "./FlightHero";
async function loadScene() {
  const { createFlightScene } = await import("./flightScene");
  return createFlightScene;
}
export function SpaceshipHero() {
  return (
    <FlightHero
      loadScene={loadScene}
      description="A detailed spacecraft assembles on a launch platform, lifts off, and accelerates through streaks of blue and white stars."
    />
  );
}

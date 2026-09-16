import { FlightHero } from "./FlightHero";
async function loadScene() {
  const { createAircraftScene } = await import("./aircraftScene");
  return createAircraftScene;
}
export function AircraftHero() {
  return (
    <FlightHero
      loadScene={loadScene}
      description="A silver aircraft assembles, takes off, and banks through softly lit clouds across the sky."
    />
  );
}

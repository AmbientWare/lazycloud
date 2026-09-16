import * as THREE from "three";
import { createHeroScene } from "./heroScene";
import { createAircraft } from "./aircraftModel";
import { createCloudField } from "./cloudField";

export function createAircraftScene(canvas: HTMLCanvasElement, stage: HTMLDivElement) {
  return createHeroScene(canvas, stage, (scene) => {
    const { aircraft, parts } = createAircraft();
    const { clouds, update: updateClouds } = createCloudField();
    scene.add(aircraft, clouds);
    const runway = new THREE.Group();
    const deck = new THREE.Mesh(
      new THREE.BoxGeometry(3.3, 8, 0.08),
      new THREE.MeshStandardMaterial({ color: "#142431", metalness: 0.55, roughness: 0.5 }),
    );
    runway.add(deck);
    const guide = new THREE.MeshBasicMaterial({
      color: "#59bdd8",
      transparent: true,
      opacity: 0.35,
    });
    for (const side of [-1, 1]) {
      const line = new THREE.Mesh(new THREE.PlaneGeometry(0.015, 7.6), guide);
      line.position.set(side * 1.5, 0, 0.05);
      runway.add(line);
    }
    for (let index = 0; index < 9; index++) {
      const line = new THREE.Mesh(new THREE.PlaneGeometry(0.03, 0.32), guide);
      line.position.set(0, index * 0.8 - 3.4, 0.05);
      runway.add(line);
    }
    scene.add(runway);
    const start = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.1, 0, -0.15));
    const cruise = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.28, -0.25, -0.55));
    return {
      update(elapsed: number, reducedMotion: boolean) {
        const takeoff = THREE.MathUtils.smoothstep(elapsed, 1.3, 2.8);
        for (const part of parts) {
          const remaining = 1 - THREE.MathUtils.smoothstep(elapsed, part.delay, part.delay + 0.48);
          part.group.position.copy(part.offset).multiplyScalar(remaining);
          part.group.rotation.y = part.offset.x * remaining * 0.22;
        }
        aircraft.quaternion.copy(start).slerp(cruise, takeoff);
        aircraft.position.set(
          takeoff * 0.25,
          takeoff * 0.25 + (reducedMotion ? 0 : Math.sin(elapsed * 0.8) * 0.04),
          0,
        );
        aircraft.scale.setScalar(1.06);
        runway.rotation.z = -0.15;
        runway.position.set(-takeoff * 2, -takeoff * 9, -0.65 - takeoff * 4);
        runway.visible = takeoff < 1;
        updateClouds(elapsed, reducedMotion);
      },
    };
  });
}

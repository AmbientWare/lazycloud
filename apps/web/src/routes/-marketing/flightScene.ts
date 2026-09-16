import * as THREE from "three";
import { createHeroScene } from "./heroScene";
import { createShip } from "./shipModel";
import { createLaunchPlatform } from "./launchPlatform";
import { createStarfield } from "./starfield";
export function createFlightScene(canvas: HTMLCanvasElement, stage: HTMLDivElement) {
  return createHeroScene(canvas, stage, (scene) => {
    const { ship, parts, exhaust, exhaustMaterial, engineLight } = createShip();
    const { platform, clamps, wash } = createLaunchPlatform();
    const { stars, update: updateStars } = createStarfield();
    scene.add(ship, platform, stars);
    const launchRotation = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, -0.35, 0));
    const flightRotation = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.24, -0.44, -0.62));

    return {
      update(elapsed: number, reducedMotion: boolean) {
        const ignition = THREE.MathUtils.smoothstep(elapsed, 1.05, 1.35);
        const lift = THREE.MathUtils.smoothstep(elapsed, 1.35, 2.5);
        const flight = THREE.MathUtils.smoothstep(elapsed, 1.8, 3.2);
        for (const part of parts) {
          const remaining = 1 - THREE.MathUtils.smoothstep(elapsed, part.delay, part.delay + 0.44);
          part.group.position.copy(part.offset).multiplyScalar(remaining);
          part.group.rotation.set(
            part.spin.x * remaining,
            part.spin.y * remaining,
            part.spin.z * remaining,
          );
        }
        ship.quaternion.copy(launchRotation).slerp(flightRotation, flight);
        const cruiseTime = Math.max(0, elapsed - 3.2);
        ship.position.set(flight * 0.35, lift * 0.45 + Math.sin(cruiseTime * 1.1) * 0.055, 0);
        ship.scale.setScalar(1.05 + flight * 0.17);
        exhaust.visible = ignition > 0;
        exhaust.scale.y = ignition * (0.18 + lift * 0.9 + flight * 0.25);
        exhaustMaterial.uniforms.time.value = elapsed;
        engineLight.intensity = ignition * (0.4 + flight * 0.8);
        platform.position.set(-flight * 2, -1.9 - lift * 7, -lift * 3);
        platform.scale.setScalar(0.78 - lift * 0.3);
        platform.visible = lift < 1;
        const release = THREE.MathUtils.smoothstep(elapsed, 1.1, 1.4);
        clamps.forEach((clamp, index) => {
          const angle = (index / 3) * Math.PI * 2;
          clamp.position.set(Math.sin(angle) * release * 0.5, 0, Math.cos(angle) * release * 0.5);
        });
        wash.material.uniforms.strength.value = ignition * (1 - lift);
        updateStars(elapsed, flight, reducedMotion);
      },
    };
  });
}

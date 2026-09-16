import * as THREE from "three";

export function createStarfield() {
  const stars = new THREE.Group();
  stars.rotation.z = -0.62;
  const count = 220;
  const positions = new Float32Array(count * 6);
  const colors = new Float32Array(count * 6);
  const seeds: { x: number; y: number; z: number; speed: number; length: number }[] = [];
  let seed = 73;
  const random = () => {
    seed = (seed * 16807) % 2147483647;
    return (seed - 1) / 2147483646;
  };
  for (let index = 0; index < count; index++) {
    seeds.push({
      x: (random() - 0.5) * 32,
      y: random() * 42,
      z: -3 - random() * 22,
      speed: 10 + random() * 16,
      length: 0.5 + random() * 2.4,
    });
    const brightness = 0.25 + random() * 0.5;
    const color = new THREE.Color(index % 4 === 0 ? "#45bbef" : "#b6d8e9");
    for (let end = 0; end < 2; end++) {
      const offset = index * 6 + end * 3;
      const fade = end === 0 ? 0.06 : brightness;
      colors[offset] = color.r * fade;
      colors[offset + 1] = color.g * fade;
      colors[offset + 2] = color.b * fade;
    }
  }
  const geometry = new THREE.BufferGeometry();
  const attribute = new THREE.BufferAttribute(positions, 3).setUsage(THREE.DynamicDrawUsage);
  geometry.setAttribute("position", attribute);
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  const material = new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.3,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  const streaks = new THREE.LineSegments(geometry, material);
  streaks.frustumCulled = false;
  stars.add(streaks);

  return {
    stars,
    update(time: number, flight: number, reducedMotion: boolean) {
      // Integrating the smooth acceleration keeps stars continuous through launch.
      const t = THREE.MathUtils.clamp((time - 1.8) / 1.4, 0, 1);
      const distance = 1.4 * (t ** 3 - 0.5 * t ** 4) + Math.max(0, time - 3.2);
      for (let index = 0; index < count; index++) {
        const star = seeds[index];
        const y = 21 - ((star.y + distance * star.speed) % 42);
        const length = reducedMotion ? 0.025 : 0.025 + flight * star.length;
        const offset = index * 6;
        positions[offset] = positions[offset + 3] = star.x;
        positions[offset + 1] = y + length;
        positions[offset + 4] = y;
        positions[offset + 2] = positions[offset + 5] = star.z;
      }
      attribute.needsUpdate = true;
      material.opacity = 0.35 + flight * 0.55;
    },
  };
}

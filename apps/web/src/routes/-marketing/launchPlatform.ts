import * as THREE from "three";

export function createLaunchPlatform() {
  const platform = new THREE.Group();
  const alloy = new THREE.MeshStandardMaterial({
    color: "#203341",
    metalness: 0.8,
    roughness: 0.4,
  });
  const graphite = new THREE.MeshStandardMaterial({
    color: "#0c1721",
    metalness: 0.6,
    roughness: 0.5,
  });
  const light = new THREE.MeshBasicMaterial({ color: "#67d0ef", transparent: true, opacity: 0.65 });
  const base = new THREE.Mesh(new THREE.CylinderGeometry(2.3, 2.05, 0.22, 64), alloy);
  base.position.y = -0.14;
  platform.add(base);
  const deck = new THREE.Mesh(new THREE.CylinderGeometry(2.05, 2.18, 0.08, 64), graphite);
  platform.add(deck);
  for (const radius of [0.68, 1.3, 2.05]) {
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(radius, radius > 2 ? 0.014 : 0.009, 6, 96),
      light,
    );
    ring.rotation.x = Math.PI / 2;
    ring.position.y = 0.051;
    platform.add(ring);
  }
  for (let i = 0; i < 24; i++) {
    const angle = (i / 24) * Math.PI * 2;
    const tick = new THREE.Mesh(
      new THREE.BoxGeometry(0.02, 0.015, i % 3 === 0 ? 0.2 : 0.08),
      light,
    );
    tick.position.set(Math.cos(angle) * 1.84, 0.052, Math.sin(angle) * 1.84);
    tick.rotation.y = Math.PI / 2 - angle;
    platform.add(tick);
  }
  const clamps: THREE.Group[] = [];
  for (let i = 0; i < 3; i++) {
    const arm = new THREE.Group();
    arm.rotation.y = (i / 3) * Math.PI * 2;
    const base = new THREE.Mesh(new THREE.BoxGeometry(0.24, 0.16, 0.7), alloy);
    base.position.set(0, 0.1, 0.9);
    const upright = new THREE.Mesh(new THREE.BoxGeometry(0.13, 0.5, 0.15), alloy);
    upright.position.set(0, 0.3, 0.6);
    arm.add(base, upright);
    platform.add(arm);
    clamps.push(arm);
  }
  const wash = new THREE.Mesh(
    new THREE.CircleGeometry(0.65, 48),
    new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      uniforms: { strength: { value: 0 } },
      vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
      fragmentShader: `varying vec2 vUv; uniform float strength; void main() { float d = length(vUv - 0.5) * 2.0; gl_FragColor = vec4(0.3, 0.8, 1.0, pow(max(1.0 - d, 0.0), 2.0) * strength); }`,
    }),
  );
  wash.rotation.x = -Math.PI / 2;
  wash.position.y = 0.065;
  platform.add(wash);
  return { platform, clamps, wash };
}

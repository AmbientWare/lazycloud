import * as THREE from "three";

export function createEngineExhaust() {
  const exhaust = new THREE.Group();
  const material = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    uniforms: { time: { value: 0 } },
    vertexShader: `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      varying vec2 vUv;
      uniform float time;
      void main() {
        float distance = 1.0 - vUv.y;
        float x = vUv.x - 0.5;
        float width = 0.018 + 0.065 * pow(vUv.y, 0.8);
        float core = exp(-pow(x / (width * 0.38), 2.0));
        float glow = exp(-pow(x / (width * 1.8), 2.0));
        float diamonds = 0.86 + 0.14 * cos(distance * 36.0 - time * 2.0);
        float fade = pow(vUv.y, 1.35) * smoothstep(0.0, 0.035, distance);
        vec3 color = vec3(0.07, 0.39, 1.0) * glow * 0.55;
        color += vec3(0.42, 0.86, 1.0) * core;
        color += vec3(0.68, 0.8, 0.85) * pow(core, 3.0) * pow(vUv.y, 2.0);
        gl_FragColor = vec4(color * diamonds, fade * (glow * 0.55 + core * 0.75));
      }
    `,
  });
  const geometry = new THREE.PlaneGeometry(1.8, 3.7);
  for (const side of [-1, 0, 1]) {
    const plume = new THREE.Mesh(geometry, material);
    const scale = side === 0 ? 1 : 0.6;
    plume.scale.set(scale, scale, scale);
    plume.position.set(side * 0.97, -1.85 * scale - (side === 0 ? 0 : 0.06), 0.12);
    exhaust.add(plume);
    const cross = plume.clone();
    cross.rotation.y = Math.PI / 2;
    exhaust.add(cross);
  }
  exhaust.position.y = -1.49;
  return { exhaust, material };
}

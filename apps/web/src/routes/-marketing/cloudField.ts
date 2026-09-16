import * as THREE from "three";

export function createCloudField() {
  const clouds = new THREE.Group();
  clouds.rotation.z = -0.55;
  const geometry = new THREE.PlaneGeometry(1, 1);
  let seed = 41;
  const random = () => {
    seed = (seed * 16807) % 2147483647;
    return (seed - 1) / 2147483646;
  };
  const layers = Array.from({ length: 22 }, (_, index) => {
    const near = index > 16;
    const material = new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      uniforms: { seed: { value: index * 3.71 }, opacity: { value: 0 } },
      vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
      fragmentShader: `
        varying vec2 vUv;
        uniform float seed;
        uniform float opacity;
        float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
        float noise(vec2 p) {
          vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
          return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y);
        }
        float fbm(vec2 p) { return noise(p) * 0.55 + noise(p * 2.07) * 0.28 + noise(p * 4.13) * 0.17; }
        void main() {
          vec2 p = (vUv - 0.5) * vec2(2.5, 2.0);
          float shape = 0.0;
          for (int i = 0; i < 5; i++) {
            float a = float(i);
            vec2 center = vec2((a - 2.0) * 0.33, sin(a * 2.4 + seed) * 0.16);
            vec2 d = (p - center) / vec2(0.42, 0.36 + hash(vec2(a, seed)) * 0.16);
            shape = max(shape, exp(-dot(d, d) * 1.5));
          }
          float detail = fbm(p * 5.0 + seed);
          float density = smoothstep(0.04, 0.65, shape * (0.65 + detail * 0.75));
          float edge = smoothstep(0.0, 0.14, vUv.x) * smoothstep(0.0, 0.14, 1.0 - vUv.x);
          edge *= smoothstep(0.0, 0.16, vUv.y) * smoothstep(0.0, 0.16, 1.0 - vUv.y);
          float lighting = clamp(0.28 + vUv.y * 0.55 + detail * 0.25, 0.0, 1.0);
          vec3 color = mix(vec3(0.012, 0.025, 0.046), vec3(0.16, 0.26, 0.37), lighting * lighting);
          gl_FragColor = vec4(color, density * edge * opacity);
        }
      `,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.rotation.z = 0.55 + (random() - 0.5) * 0.15;
    const width = near ? 4 + random() * 3 : 6 + random() * 7;
    mesh.scale.set(width, width * (0.48 + random() * 0.22), 1);
    const x = (random() - 0.62) * 28;
    const y = random() * 28;
    mesh.position.set(x, y - 14, near ? 1 + random() * 2 : -4 - random() * 12);
    clouds.add(mesh);
    return {
      mesh,
      y,
      speed: near ? 1.6 + random() : 0.35 + random() * 0.5,
      opacity: near ? 0.2 : 0.5,
    };
  });
  return {
    clouds,
    update(time: number, reducedMotion: boolean) {
      const reveal = THREE.MathUtils.smoothstep(time, 1.3, 3);
      const travel = reducedMotion ? 0 : Math.max(0, time - 1.3);
      for (const layer of layers) {
        layer.mesh.position.y = 14 - ((layer.y + travel * layer.speed) % 28);
        layer.mesh.material.uniforms.opacity.value = reveal * layer.opacity;
      }
    },
  };
}

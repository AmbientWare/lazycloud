import * as THREE from "three";

export function createShip() {
  const ship = new THREE.Group();
  const shell = new THREE.MeshPhysicalMaterial({
    color: "#a9bdcb",
    metalness: 0.82,
    roughness: 0.27,
    clearcoat: 0.5,
  });
  const edge = new THREE.MeshStandardMaterial({
    color: "#293e50",
    metalness: 0.85,
    roughness: 0.3,
  });
  const inner = new THREE.MeshStandardMaterial({
    color: "#0c1925",
    metalness: 0.75,
    roughness: 0.25,
  });
  const energy = new THREE.MeshBasicMaterial({ color: "#93eaff" });
  const blue = new THREE.MeshStandardMaterial({
    color: "#2485a5",
    emissive: "#197997",
    emissiveIntensity: 0.6,
    metalness: 0.6,
    roughness: 0.3,
  });
  const parts: { group: THREE.Group; offset: THREE.Vector3; spin: THREE.Vector3; delay: number }[] =
    [];
  const exhaust = new THREE.Group();

  function part(x: number, y: number, z: number, delay: number) {
    const group = new THREE.Group();
    ship.add(group);
    parts.push({
      group,
      offset: new THREE.Vector3(x, y, z).multiplyScalar(0.25),
      spin: new THREE.Vector3(),
      delay: delay * 0.12,
    });
    return group;
  }
  function plate(
    parent: THREE.Group,
    points: [number, number][],
    depth: number,
    material: THREE.Material,
    z = 0,
  ) {
    const panels = new THREE.Group();
    parent.add(panels);
    const low = Math.min(...points.map((point) => point[1]));
    const high = Math.max(...points.map((point) => point[1]));
    const count = Math.ceil((high - low) / 0.38);
    for (let index = 0; index < count; index++) {
      const bottom = low + ((high - low) * index) / count + 0.012;
      const top = low + ((high - low) * (index + 1)) / count - 0.012;
      const polygon = clipHorizontal(clipHorizontal(points, bottom, true), top, false);
      if (polygon.length < 3) continue;
      const center = new THREE.Vector3(
        polygon.reduce((sum, point) => sum + point[0], 0) / polygon.length,
        (bottom + top) / 2,
        z,
      );
      const shape = new THREE.Shape(
        polygon.map(([x, y]) => new THREE.Vector2(x - center.x, y - center.y)),
      );
      const geometry = new THREE.ExtrudeGeometry(shape, {
        depth,
        bevelEnabled: true,
        bevelSegments: 2,
        bevelSize: 0.01,
        bevelThickness: 0.012,
      });
      geometry.translate(0, 0, -depth / 2);
      const panel = new THREE.Mesh(geometry, material);
      const pivot = new THREE.Group();
      pivot.position.copy(center);
      const chunk = new THREE.Group();
      chunk.add(panel);
      pivot.add(chunk);
      panels.add(pivot);
      const serial = parts.length;
      const direction = center.x > 0.05 ? 1 : serial % 2 ? 1 : -1;
      parts.push({
        group: chunk,
        offset: new THREE.Vector3(
          direction * (0.8 + (serial % 5) * 0.23),
          (serial % 4) * 0.24,
          0.3 + (serial % 3) * 0.27,
        ),
        spin: new THREE.Vector3(((serial % 3) - 1) * 0.3, direction * 0.55, direction * 0.22),
        delay: 0.1 + ((serial * 7) % 17) * 0.032,
      });
    }
    return panels;
  }

  const spine = part(0, 0.3, -0.7, 0);
  plate(
    spine,
    [
      [0, 1.75],
      [0.29, 0.2],
      [0.38, -1.05],
      [0, -1.42],
      [-0.38, -1.05],
      [-0.29, 0.2],
    ],
    0.36,
    inner,
    -0.12,
  );
  const core = new THREE.Mesh(new THREE.CylinderGeometry(0.085, 0.14, 1.7, 8), energy);
  core.position.set(0, -0.05, 0.13);
  spine.add(core);
  for (const y of [-0.6, -0.2, 0.2, 0.6]) {
    const band = new THREE.Mesh(new THREE.TorusGeometry(0.18, 0.025, 6, 6), edge);
    band.rotation.x = Math.PI / 2;
    band.position.set(0, y, 0.07);
    spine.add(band);
  }
  for (const side of [-1, 1]) {
    const armor = part(side * 1.8, 0.55, 0.3, side < 0 ? 0.4 : 0.9);
    const surface = plate(
      armor,
      [
        [0.13, 1.55],
        [0.43, 0.76],
        [0.62, -0.15],
        [1.06, -0.95],
        [0.94, -1.39],
        [0.5, -1.17],
        [0.19, -0.65],
      ],
      0.26,
      shell,
      0.12,
    );
    surface.scale.x = side;
    const under = plate(
      armor,
      [
        [0.24, 1.05],
        [0.57, -0.23],
        [1.06, -0.97],
        [0.94, -1.39],
        [0.4, -1.12],
      ],
      0.15,
      edge,
      -0.12,
    );
    under.scale.x = side;
    const inlay = plate(
      armor,
      [
        [0.32, 0.75],
        [0.39, 0.47],
        [0.52, -0.3],
        [0.83, -0.92],
        [0.75, -0.91],
        [0.45, -0.28],
      ],
      0.01,
      energy,
      0.282,
    );
    inlay.scale.x = side;
    const fin = part(side * 2.3, -0.7, -0.5, side < 0 ? 1.25 : 1.6);
    const vane = plate(
      fin,
      [
        [0.57, -0.29],
        [1.55, -1.35],
        [1.48, -1.64],
        [0.76, -1.04],
      ],
      0.085,
      inner,
      -0.25,
    );
    vane.scale.x = side;
    const tip = plate(
      fin,
      [
        [1.32, -1.16],
        [1.52, -1.38],
        [1.47, -1.57],
        [1.37, -1.41],
      ],
      0.09,
      blue,
      -0.25,
    );
    tip.scale.x = side;
  }
  const nose = part(0, 1.75, 0.3, 1.9);
  plate(
    nose,
    [
      [0, 2.01],
      [0.19, 1.24],
      [0.1, 0.84],
      [0, 1.1],
      [-0.1, 0.84],
      [-0.19, 1.24],
    ],
    0.31,
    shell,
    0.03,
  );
  const engine = part(0, -0.65, 0, 0.2);
  const engineShell = new THREE.Mesh(
    new THREE.CylinderGeometry(0.29, 0.35, 0.42, 8, 1, true),
    edge,
  );
  engineShell.position.y = -1.25;
  engine.add(engineShell);
  for (const radius of [0.18, 0.29]) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(radius, 0.022, 8, 48), energy);
    ring.rotation.x = Math.PI / 2;
    ring.position.y = -1.48;
    engine.add(ring);
  }
  const plume = new THREE.Mesh(
    new THREE.CylinderGeometry(0.21, 0.025, 2.6, 32, 1, true),
    new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending,
      vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
      fragmentShader: `varying vec2 vUv; void main() { float fade = pow(vUv.y, 1.8); gl_FragColor = vec4(mix(vec3(0.1, 0.45, 0.85), vec3(0.7, 0.95, 1.0), vUv.y), fade * 0.65); }`,
    }),
  );
  plume.position.y = -1.3;
  exhaust.add(plume);
  exhaust.position.y = -1.49;
  engine.add(exhaust);
  return { ship, parts, exhaust };
}

function clipHorizontal(
  polygon: [number, number][],
  height: number,
  keepAbove: boolean,
): [number, number][] {
  const result: [number, number][] = [];
  for (let index = 0; index < polygon.length; index++) {
    const a = polygon[index];
    const b = polygon[(index + 1) % polygon.length];
    const insideA = keepAbove ? a[1] >= height : a[1] <= height;
    const insideB = keepAbove ? b[1] >= height : b[1] <= height;
    if (insideA) result.push(a);
    if (insideA !== insideB) {
      const t = (height - a[1]) / (b[1] - a[1]);
      result.push([a[0] + (b[0] - a[0]) * t, height]);
    }
  }
  return result;
}

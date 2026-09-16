import * as THREE from "three";
import { createEngineExhaust } from "./engineExhaust";

export function createShip() {
  const ship = new THREE.Group();
  const shell = new THREE.MeshPhysicalMaterial({
    color: "#b8c8d5",
    metalness: 0.8,
    roughness: 0.26,
    clearcoat: 1,
    clearcoatRoughness: 0.16,
  });
  const edge = new THREE.MeshStandardMaterial({
    color: "#14212e",
    metalness: 0.92,
    roughness: 0.24,
  });
  const inner = new THREE.MeshStandardMaterial({
    color: "#080f18",
    metalness: 0.55,
    roughness: 0.36,
  });
  const energy = new THREE.MeshStandardMaterial({
    color: "#73dfff",
    emissive: "#39bfe9",
    emissiveIntensity: 0.8,
    roughness: 0.22,
    metalness: 0.3,
  });
  const blue = new THREE.MeshStandardMaterial({
    color: "#2485a5",
    emissive: "#197997",
    emissiveIntensity: 0.6,
    metalness: 0.6,
    roughness: 0.3,
  });
  const parts: { group: THREE.Group; offset: THREE.Vector3; spin: THREE.Vector3; delay: number }[] =
    [];
  const glass = new THREE.MeshPhysicalMaterial({
    color: "#063450",
    metalness: 0.65,
    roughness: 0.14,
    clearcoat: 0.6,
    clearcoatRoughness: 0.03,
    iridescence: 0.18,
    iridescenceIOR: 1.3,
    envMapIntensity: 0.7,
  });
  const { exhaust, material: exhaustMaterial } = createEngineExhaust();

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
    panelHeight = 0.38,
  ) {
    const panels = new THREE.Group();
    parent.add(panels);
    const low = Math.min(...points.map((point) => point[1]));
    const high = Math.max(...points.map((point) => point[1]));
    const count = Math.ceil((high - low) / panelHeight);
    for (let index = 0; index < count; index++) {
      const bottom = low + ((high - low) * index) / count + 0.009;
      const top = low + ((high - low) * (index + 1)) / count - 0.009;
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
        bevelSegments: 3,
        bevelSize: 0.007,
        bevelThickness: 0.008,
      });
      geometry.translate(0, 0, -depth / 2);
      const positions = geometry.getAttribute("position");
      for (let vertex = 0; vertex < positions.count; vertex++) {
        const x = positions.getX(vertex) + center.x;
        const y = positions.getY(vertex) + center.y;
        positions.setZ(
          vertex,
          positions.getZ(vertex) + 0.22 * Math.cos(x * 1.8) + 0.06 * Math.cos(y * 1.5),
        );
      }
      geometry.computeVertexNormals();
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
  const core = new THREE.Mesh(new THREE.CylinderGeometry(0.085, 0.14, 1.7, 8), blue);
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
  const cockpit = part(0, 0.8, 1.3, 0.7);
  plate(
    cockpit,
    [
      [0, 1.08],
      [0.19, 0.54],
      [0.16, 0.04],
      [0, -0.12],
      [-0.16, 0.04],
      [-0.19, 0.54],
    ],
    0.14,
    edge,
    0.34,
    1.4,
  );
  const canopy = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 16), glass);
  canopy.scale.set(0.15, 0.49, 0.17);
  canopy.position.set(0, 0.47, 0.66);
  cockpit.add(canopy);
  for (const side of [-1, 1]) {
    const pod = part(side * 3.1, -0.8, 0.8, 1.2);
    const housing = new THREE.Mesh(new THREE.CylinderGeometry(0.13, 0.2, 0.95, 8), shell);
    housing.position.set(side * 0.97, -0.9, 0.12);
    pod.add(housing);
    for (let index = 0; index < 5; index++) {
      const collar = new THREE.Mesh(
        new THREE.TorusGeometry(0.17 + index * 0.006, 0.025, 4, 8),
        edge,
      );
      collar.rotation.x = Math.PI / 2;
      collar.position.set(side * 0.97, -0.97 - index * 0.09, 0.12);
      pod.add(collar);
    }
    const nozzle = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.12, 0.16, 12), inner);
    nozzle.position.set(side * 0.97, -1.46, 0.12);
    pod.add(nozzle);
    const nozzleLight = new THREE.Mesh(new THREE.TorusGeometry(0.115, 0.018, 6, 32), energy);
    nozzleLight.rotation.x = Math.PI / 2;
    nozzleLight.position.set(side * 0.97, -1.55, 0.12);
    pod.add(nozzleLight);
    const stabilizer = part(side * 2, 0.4, -1.2, 1.3);
    const blade = plate(
      stabilizer,
      [
        [0.42, -0.25],
        [0.58, 0.65],
        [0.72, 0.22],
        [0.72, -0.95],
      ],
      0.08,
      shell,
      -0.24,
    );
    blade.scale.x = side;
    for (let index = 0; index < 5; index++) {
      const ventPart = part(side * 1.4, 0.3, 1.8, index * 0.5);
      const vent = new THREE.Mesh(new THREE.BoxGeometry(0.13, 0.026, 0.025), inner);
      vent.position.set(side * (0.5 + index * 0.045), -0.39 - index * 0.1, 0.265);
      vent.rotation.z = side * -0.4;
      ventPart.add(vent);
    }
  }
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
  ship.traverse((node) => {
    if (node instanceof THREE.Mesh) {
      node.castShadow = true;
      node.receiveShadow = true;
    }
  });
  engine.add(exhaust);
  const engineLight = new THREE.PointLight("#47bfff", 0, 3.5, 2);
  engineLight.position.set(0, -1.65, 0.65);
  ship.add(engineLight);
  return { ship, parts, exhaust, exhaustMaterial, engineLight };
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

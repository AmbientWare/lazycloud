import * as THREE from "three";

export function createAircraft() {
  const aircraft = new THREE.Group();
  const silver = new THREE.MeshPhysicalMaterial({
    color: "#c6d3df",
    metalness: 0.76,
    roughness: 0.24,
    clearcoat: 0.8,
  });
  const graphite = new THREE.MeshStandardMaterial({
    color: "#101e2c",
    metalness: 0.85,
    roughness: 0.28,
  });
  const wingMaterial = new THREE.MeshPhysicalMaterial({
    color: "#91a6b8",
    metalness: 0.7,
    roughness: 0.36,
    clearcoat: 0.6,
    envMapIntensity: 0.45,
  });
  const titanium = new THREE.MeshStandardMaterial({
    color: "#657e94",
    metalness: 0.95,
    roughness: 0.32,
  });
  const glass = new THREE.MeshPhysicalMaterial({
    color: "#07435d",
    metalness: 0.55,
    roughness: 0.09,
    clearcoat: 1,
    envMapIntensity: 0.8,
  });
  const blue = new THREE.MeshStandardMaterial({
    color: "#55c3e6",
    emissive: "#2184a8",
    emissiveIntensity: 0.5,
    metalness: 0.6,
    roughness: 0.25,
  });
  const parts: { group: THREE.Group; offset: THREE.Vector3; delay: number }[] = [];
  function part(side: number) {
    const group = new THREE.Group();
    aircraft.add(group);
    const index = parts.length;
    parts.push({
      group,
      offset: new THREE.Vector3(
        side * (0.8 + (index % 3) * 0.4),
        ((index % 4) - 1) * 0.25,
        0.5 + (index % 3) * 0.25,
      ),
      delay: ((index * 7) % 19) * 0.025,
    });
    return group;
  }
  function panel(
    parent: THREE.Group,
    points: [number, number][],
    material: THREE.Material,
    depth = 0.06,
  ) {
    const shape = new THREE.Shape(points.map(([x, y]) => new THREE.Vector2(x, y)));
    const geometry = new THREE.ExtrudeGeometry(shape, {
      depth,
      bevelEnabled: true,
      bevelSize: 0.018,
      bevelThickness: 0.016,
      bevelSegments: 3,
    });
    geometry.translate(0, 0, -depth / 2);
    const vertices = geometry.getAttribute("position");
    for (let index = 0; index < vertices.count; index++) {
      vertices.setZ(
        index,
        vertices.getZ(index) +
          0.045 * Math.sin(vertices.getX(index) * 1.8) +
          0.02 * Math.cos(vertices.getY(index) * 2),
      );
    }
    geometry.computeVertexNormals();
    const mesh = new THREE.Mesh(geometry, material);
    parent.add(mesh);
    return mesh;
  }
  const stations = [
    [-2.05, 0.13],
    [-1.62, 0.28],
    [-1.1, 0.36],
    [-0.5, 0.39],
    [0.15, 0.37],
    [0.75, 0.3],
    [1.3, 0.23],
    [1.8, 0.14],
    [2.5, 0.006],
  ];
  for (let index = 0; index < stations.length - 1; index++) {
    const [y0, radius0] = stations[index];
    const [y1, radius1] = stations[index + 1];
    const points = Array.from({ length: 7 }, (_, step) => {
      const t = step / 6;
      return new THREE.Vector2(
        THREE.MathUtils.lerp(radius0, radius1, t),
        THREE.MathUtils.lerp(y0 + 0.006, y1 - 0.006, t),
      );
    });
    const body = new THREE.Mesh(new THREE.LatheGeometry(points, 48), silver);
    body.scale.z = 0.82;
    part(index % 2 ? -1 : 1).add(body);
  }
  for (const side of [-1, 1]) {
    const wing = part(side);
    const surface = panel(
      wing,
      [
        [0.28, 0.55],
        [2.7, -1.05],
        [2.55, -1.43],
        [0.34, -0.85],
      ],
      wingMaterial,
      0.085,
    );
    surface.scale.x = side;
    surface.rotation.y = side * -0.035;
    const slat = part(side);
    const leadingEdge = panel(
      slat,
      [
        [0.5, 0.38],
        [2.65, -1.06],
        [2.61, -1.16],
        [0.49, 0.27],
      ],
      titanium,
      0.032,
    );
    leadingEdge.scale.x = side;
    leadingEdge.position.z = 0.063;
    for (let index = 0; index < 2; index++) {
      const flap = part(side);
      const x = 0.56 + index * 0.84;
      const mesh = panel(
        flap,
        [
          [x, -0.9 - index * 0.22],
          [x + 0.78, -1.11 - index * 0.22],
          [x + 0.71, -1.26 - index * 0.2],
          [x, -1.04 - index * 0.2],
        ],
        graphite,
        0.036,
      );
      mesh.scale.x = side;
      mesh.position.z = 0.048;
    }
    const tip = part(side);
    const winglet = panel(
      tip,
      [
        [2.42, -1.08],
        [2.78, -0.73],
        [2.68, -1.35],
        [2.48, -1.45],
      ],
      blue,
      0.045,
    );
    winglet.scale.x = side;
    winglet.position.z = 0.055;
    const tail = part(side);
    const stabilizer = panel(
      tail,
      [
        [0.2, -1.35],
        [1.18, -1.92],
        [1.08, -2.18],
        [0.16, -1.98],
      ],
      wingMaterial,
    );
    stabilizer.scale.x = side;
    stabilizer.position.z = 0.04;
    const fin = panel(
      tail,
      [
        [0, -1.02],
        [0.61, -1.69],
        [0.5, -2.06],
        [0, -1.85],
      ],
      titanium,
    );
    fin.position.set(side * 0.2, 0, 0.15);
    fin.rotation.y = side * -1.02;
    fin.scale.x = side;
    const engine = part(side);
    const housing = new THREE.Mesh(new THREE.CylinderGeometry(0.19, 0.23, 1.2, 32), titanium);
    housing.position.set(side * 0.34, -1.34, -0.06);
    engine.add(housing);
    for (let ring = 0; ring < 3; ring++) {
      const collar = new THREE.Mesh(new THREE.TorusGeometry(0.22, 0.02, 8, 32), graphite);
      collar.rotation.x = Math.PI / 2;
      collar.position.set(side * 0.34, -1.72 - ring * 0.085, -0.06);
      engine.add(collar);
    }
    const nozzle = new THREE.Mesh(new THREE.CircleGeometry(0.17, 32), blue);
    nozzle.rotation.x = Math.PI / 2;
    nozzle.position.set(side * 0.34, -1.96, -0.06);
    engine.add(nozzle);
  }
  const cockpit = part(1);
  const frame = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 20), graphite);
  frame.scale.set(0.215, 0.66, 0.205);
  frame.position.set(0, 0.83, 0.23);
  cockpit.add(frame);
  const canopy = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 20), glass);
  canopy.scale.set(0.19, 0.625, 0.19);
  canopy.position.set(0, 0.85, 0.265);
  cockpit.add(canopy);
  aircraft.traverse((node) => {
    if (node instanceof THREE.Mesh) {
      node.castShadow = true;
      node.receiveShadow = true;
    }
  });
  return { aircraft, parts };
}

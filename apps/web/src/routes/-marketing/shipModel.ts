import * as THREE from "three";

export function createShip() {
  const ship = new THREE.Group();
  const ceramic = new THREE.MeshPhysicalMaterial({
    color: "#dce5ec",
    metalness: 0.62,
    roughness: 0.24,
    clearcoat: 0.8,
  });
  const metal = new THREE.MeshStandardMaterial({
    color: "#526477",
    metalness: 0.9,
    roughness: 0.28,
  });
  const dark = new THREE.MeshStandardMaterial({
    color: "#101e2d",
    metalness: 0.7,
    roughness: 0.22,
  });
  const blue = new THREE.MeshPhysicalMaterial({
    color: "#41c4ed",
    metalness: 0.5,
    roughness: 0.2,
    clearcoat: 1,
  });
  const light = new THREE.MeshBasicMaterial({ color: "#9aeaff" });
  const glass = new THREE.MeshPhysicalMaterial({
    color: "#072b48",
    metalness: 0.75,
    roughness: 0.1,
    clearcoat: 1,
  });
  const parts: { group: THREE.Group; offset: THREE.Vector3; delay: number }[] = [];
  const flames: THREE.Mesh<THREE.ConeGeometry, THREE.MeshBasicMaterial>[] = [];

  function part(offset: THREE.Vector3, delay: number) {
    const group = new THREE.Group();
    ship.add(group);
    parts.push({ group, offset, delay });
    return group;
  }
  function mesh(
    parent: THREE.Group,
    geometry: THREE.BufferGeometry,
    material: THREE.Material,
    x = 0,
    y = 0,
    z = 0,
  ) {
    const result = new THREE.Mesh(geometry, material);
    result.position.set(x, y, z);
    parent.add(result);
    return result;
  }
  function ring(
    parent: THREE.Group,
    radius: number,
    tube: number,
    x: number,
    y: number,
    material: THREE.Material,
  ) {
    const result = mesh(parent, new THREE.TorusGeometry(radius, tube, 10, 64), material, x, y);
    result.rotation.x = Math.PI / 2;
    return result;
  }

  const core = part(new THREE.Vector3(0, -0.3, 0), 0);
  mesh(
    core,
    new THREE.LatheGeometry(
      [
        new THREE.Vector2(0.35, -1.4),
        new THREE.Vector2(0.5, -1.2),
        new THREE.Vector2(0.57, -0.7),
        new THREE.Vector2(0.57, 0.2),
        new THREE.Vector2(0.5, 0.95),
      ],
      64,
    ),
    ceramic,
  );
  for (const y of [-1.15, -0.72, 0.15, 0.87])
    ring(core, y > 0.8 ? 0.51 : 0.575, 0.012, 0, y, metal);
  mesh(core, new THREE.CylinderGeometry(0.581, 0.581, 0.16, 64), blue, 0, -0.58);
  for (const side of [-1, 1]) {
    mesh(core, new THREE.BoxGeometry(0.035, 0.42, 0.025), light, side * 0.38, -0.03, 0.44);
    for (let i = 0; i < 6; i++)
      mesh(
        core,
        new THREE.BoxGeometry(0.12, 0.018, 0.04),
        dark,
        side * 0.33,
        -0.94 + i * 0.05,
        0.47,
      );
  }

  const nose = part(new THREE.Vector3(0, 1.9, 0), 0.15);
  mesh(
    nose,
    new THREE.LatheGeometry(
      [
        new THREE.Vector2(0.5, 0.96),
        new THREE.Vector2(0.47, 1.2),
        new THREE.Vector2(0.36, 1.55),
        new THREE.Vector2(0.2, 1.87),
        new THREE.Vector2(0.06, 2.03),
        new THREE.Vector2(0, 2.05),
      ],
      64,
    ),
    ceramic,
  );
  const canopyFrame = mesh(nose, new THREE.SphereGeometry(1, 40, 24), metal, 0, 1.21, 0.36);
  canopyFrame.scale.set(0.34, 0.49, 0.16);
  const canopy = mesh(nose, new THREE.SphereGeometry(1, 40, 24), glass, 0, 1.23, 0.39);
  canopy.scale.set(0.295, 0.43, 0.15);
  mesh(nose, new THREE.BoxGeometry(0.018, 0.64, 0.018), metal, 0, 1.24, 0.54);

  for (const side of [-1, 1]) {
    const wing = part(new THREE.Vector3(side * 2.1, -0.5, 0.2), side === -1 ? 0.3 : 0.45);
    const shape = new THREE.Shape();
    shape.moveTo(0.42, 0.05);
    shape.bezierCurveTo(0.8, -0.25, 1.08, -0.86, 1.95, -1.36);
    shape.lineTo(2.02, -1.8);
    shape.quadraticCurveTo(1.2, -1.75, 0.4, -1.24);
    shape.closePath();
    const surface = mesh(
      wing,
      new THREE.ExtrudeGeometry(shape, {
        depth: 0.13,
        bevelEnabled: true,
        bevelSegments: 3,
        steps: 1,
        bevelSize: 0.07,
        bevelThickness: 0.06,
      }),
      ceramic,
      0,
      0,
      -0.13,
    );
    surface.scale.x = side;
    const inset = new THREE.Shape();
    inset.moveTo(0.73, -0.65);
    inset.lineTo(1.8, -1.43);
    inset.lineTo(1.82, -1.58);
    inset.lineTo(0.74, -1.12);
    inset.closePath();
    const accent = mesh(wing, new THREE.ShapeGeometry(inset), blue, 0, 0, 0.075);
    accent.scale.x = side;
    mesh(wing, new THREE.CapsuleGeometry(0.065, 0.5, 6, 12), metal, side * 1.94, -1.51, 0);
    mesh(wing, new THREE.SphereGeometry(0.05, 12, 8), light, side * 1.94, -1.22, 0.04);
  }

  const engineAssembly = part(new THREE.Vector3(0, -1.8, 0), 0.65);
  for (const x of [-0.83, 0, 0.83]) {
    const r = x === 0 ? 0.33 : 0.22;
    mesh(engineAssembly, new THREE.CylinderGeometry(r * 0.8, r, 0.72, 32), metal, x, -1.15);
    if (x !== 0) {
      mesh(engineAssembly, new THREE.CapsuleGeometry(r, 0.85, 8, 32), ceramic, x, -0.72);
      ring(engineAssembly, r + 0.005, 0.04, x, -0.56, blue);
    }
    mesh(
      engineAssembly,
      new THREE.CylinderGeometry(r * 0.75, r * 1.08, 0.24, 32, 1, true),
      dark,
      x,
      -1.57,
    );
    ring(engineAssembly, r * 1.06, 0.035, x, -1.68, metal);
    ring(engineAssembly, r * 0.86, 0.025, x, -1.69, light);
    for (const [radius, length, opacity, color] of [
      [r * 0.8, 1.45, 0.27, "#42bfff"],
      [r * 0.46, 1.05, 0.75, "#b5f4ff"],
    ] as const) {
      const material = new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });
      const flame = new THREE.Mesh(new THREE.ConeGeometry(radius, length, 32), material);
      flame.geometry.rotateZ(Math.PI);
      flame.geometry.translate(0, -length / 2, 0);
      flame.position.set(x, -1.7, 0);
      engineAssembly.add(flame);
      flames.push(flame);
    }
  }

  const labelCanvas = document.createElement("canvas");
  labelCanvas.width = 512;
  labelCanvas.height = 128;
  const context = labelCanvas.getContext("2d");
  if (!context) throw new Error("Canvas 2D is required to draw the ship markings.");
  context.fillStyle = "#172c3b";
  context.font = "600 63px sans-serif";
  context.textAlign = "center";
  context.fillText("lazycloud", 256, 72);
  context.font = "18px monospace";
  context.fillStyle = "#367d9b";
  context.fillText("L C   /   0 1", 256, 111);
  const label = new THREE.CanvasTexture(labelCanvas);
  const marking = mesh(
    core,
    new THREE.PlaneGeometry(0.81, 0.203),
    new THREE.MeshBasicMaterial({ map: label, transparent: true, depthWrite: false }),
    0,
    -0.26,
    0.579,
  );
  marking.rotation.z = 0;

  return { ship, parts, flames, label };
}

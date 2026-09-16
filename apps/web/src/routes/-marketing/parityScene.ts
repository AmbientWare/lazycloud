import * as THREE from "three";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

export type ParityKind = "local" | "cloud" | "production";

export function createParityScene(canvas: HTMLCanvasElement, kind: ParityKind) {
  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: true,
    powerPreference: "low-power",
  });
  renderer.setClearColor(0, 0);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-4.5, 4.5, 2.25, -2.25, 0.1, 30);
  camera.position.set(5, 4, 7);
  camera.lookAt(0, 0, 0);
  const room = new RoomEnvironment();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const environment = pmrem.fromScene(room, 0.04);
  scene.environment = environment.texture;
  scene.environmentIntensity = 0.65;
  room.dispose();
  pmrem.dispose();
  scene.add(new THREE.HemisphereLight("#d1eafa", "#102133", 1.5));
  const key = new THREE.DirectionalLight("#effaff", 3);
  key.position.set(-3, 6, 5);
  key.castShadow = true;
  key.shadow.mapSize.set(512, 512);
  key.shadow.camera.left = key.shadow.camera.bottom = -4;
  key.shadow.camera.right = key.shadow.camera.top = 4;
  key.shadow.normalBias = 0.03;
  scene.add(key);
  const rim = new THREE.DirectionalLight("#46bded", 2);
  rim.position.set(3, 1, -4);
  scene.add(rim);

  const object = new THREE.Group();
  scene.add(object);
  const graphite = new THREE.MeshStandardMaterial({
    color: "#263b4b",
    metalness: 0.65,
    roughness: 0.3,
  });
  const silver = new THREE.MeshStandardMaterial({
    color: "#9fbbc9",
    metalness: 0.8,
    roughness: 0.25,
  });
  const dark = new THREE.MeshStandardMaterial({ color: "#06131e", metalness: 0.2, roughness: 0.5 });
  const blue = new THREE.MeshStandardMaterial({
    color: "#61cbed",
    emissive: "#2da4d0",
    emissiveIntensity: 0.15,
    metalness: 0.35,
    roughness: 0.24,
  });
  const light = new THREE.MeshBasicMaterial({ color: "#bcf0ff" });
  const pulses: THREE.MeshStandardMaterial[] = [];

  function box(
    parent: THREE.Object3D,
    size: [number, number, number],
    at: [number, number, number],
    material: THREE.Material,
    radius = 0.04,
  ) {
    const geometry = new RoundedBoxGeometry(
      ...size,
      3,
      Math.min(radius, ...size.map((n) => n / 3)),
    );
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(...at);
    mesh.castShadow = mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  }
  function line(
    parent: THREE.Object3D,
    points: THREE.Vector3[],
    material: THREE.Material,
    radius = 0.025,
  ) {
    const curve = new THREE.CatmullRomCurve3(points);
    const mesh = new THREE.Mesh(new THREE.TubeGeometry(curve, 32, radius, 6, false), material);
    parent.add(mesh);
    return mesh;
  }
  function codeBlock(parent: THREE.Object3D, at: [number, number, number], scale = 1) {
    const group = new THREE.Group();
    group.position.set(...at);
    group.scale.setScalar(scale);
    parent.add(group);
    const material = blue.clone();
    pulses.push(material);
    box(group, [0.8, 0.8, 0.45], [0, 0, 0], material, 0.09);
    for (const direction of [-1, 1]) {
      line(
        group,
        [
          new THREE.Vector3(direction * 0.11, 0.15, 0.235),
          new THREE.Vector3(direction * 0.25, 0, 0.235),
          new THREE.Vector3(direction * 0.11, -0.15, 0.235),
        ],
        light,
        0.022,
      );
    }
    return group;
  }

  let floating: THREE.Group | undefined;
  if (kind === "local") {
    box(object, [3.1, 0.14, 1.85], [0, -0.65, 0.15], silver, 0.08);
    box(object, [2.65, 0.035, 0.68], [0, -0.56, -0.04], dark);
    for (let row = 0; row < 3; row++) {
      for (let col = 0; col < 10; col++) {
        box(
          object,
          [0.2, 0.025, 0.12],
          [-1.08 + col * 0.24, -0.535, -0.26 + row * 0.21],
          graphite,
          0.015,
        );
      }
    }
    box(object, [0.85, 0.015, 0.45], [0, -0.57, 0.67], graphite);
    const screen = new THREE.Group();
    screen.position.set(0, 0.23, -0.73);
    screen.rotation.x = -0.12;
    object.add(screen);
    box(screen, [3, 1.9, 0.14], [0, 0, 0], graphite, 0.075);
    box(screen, [2.77, 1.66, 0.015], [0, 0, 0.08], dark, 0.04);
    codeBlock(screen, [-0.64, 0.03, 0.19], 0.84);
    for (let i = 0; i < 4; i++) {
      box(
        screen,
        [i === 1 ? 0.57 : 0.76, 0.065, 0.015],
        [0.53, 0.34 - i * 0.21, 0.1],
        i === 0 ? blue : graphite,
        0.015,
      );
    }
  } else if (kind === "cloud") {
    box(object, [3, 0.16, 2.7], [0, -0.66, 0], graphite, 0.1);
    box(object, [1.7, 0.3, 1.7], [0, -0.42, 0], silver, 0.075);
    box(object, [1.42, 0.08, 1.42], [0, -0.22, 0], dark);
    for (let side = 0; side < 4; side++) {
      const pins = new THREE.Group();
      pins.rotation.y = (side * Math.PI) / 2;
      object.add(pins);
      for (let i = 0; i < 7; i++) {
        box(pins, [0.11, 0.05, 0.42], [-0.66 + i * 0.22, -0.4, 1.01], silver, 0.01);
      }
    }
    floating = codeBlock(object, [0, 0.5, 0], 1.12);
    box(object, [1.02, 0.015, 1.02], [0, -0.17, 0], blue);
  } else {
    const positions: [number, number, number][] = [
      [-1.24, -0.48, 0.66],
      [1.24, -0.48, 0.66],
      [0, -0.48, -1.15],
    ];
    const wire = new THREE.MeshStandardMaterial({
      color: "#3284a1",
      emissive: "#1a668d",
      emissiveIntensity: 0.3,
      metalness: 0.4,
      roughness: 0.4,
    });
    positions.forEach((position, i) => {
      const next = positions[(i + 1) % positions.length];
      line(
        object,
        [
          new THREE.Vector3(...position),
          new THREE.Vector3((position[0] + next[0]) / 2, -0.65, (position[2] + next[2]) / 2),
          new THREE.Vector3(...next),
        ],
        wire,
        0.035,
      );
      box(object, [1.1, 0.2, 0.95], [position[0], -0.55, position[2]], silver, 0.08);
      codeBlock(object, [position[0], 0.06, position[2]], 0.85);
    });
  }
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(20, 20),
    new THREE.ShadowMaterial({ opacity: 0.18 }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.78;
  floor.receiveShadow = true;
  scene.add(floor);

  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const pointer = new THREE.Vector2();
  const look = new THREE.Vector2();
  let elapsed = 0;
  let previousTime = 0;
  let visible = false;
  let disposed = false;

  function render(delta = 0) {
    if (motion.matches) look.set(0, 0);
    else look.lerp(pointer, 1 - Math.exp(-delta * 7));
    object.rotation.y = look.x * 0.12;
    object.rotation.x = look.y * 0.06;
    const phase = elapsed % 6;
    pulses.forEach((material, index) => {
      const t = (phase - index * 0.12) / 0.8;
      const pulse = !motion.matches && t > 0 && t < 1 ? Math.sin(t * Math.PI) ** 2 : 0;
      material.emissiveIntensity = 0.15 + pulse * 0.45;
    });
    if (floating)
      floating.position.y = 0.5 + (motion.matches ? 0 : Math.sin(elapsed * 1.2) * 0.055);
    renderer.render(scene, camera);
  }
  function tick(now: number) {
    const delta = previousTime ? (now - previousTime) / 1000 : 1 / 30;
    if (delta < 1 / 30) return;
    previousTime = now;
    elapsed += Math.min(delta, 0.05);
    render(delta);
  }
  function syncMotion() {
    previousTime = 0;
    renderer.setAnimationLoop(
      !disposed && visible && !document.hidden && !motion.matches ? tick : null,
    );
    render();
  }
  function resize() {
    const { width, height } = canvas.getBoundingClientRect();
    if (!width || !height) return;
    const halfHeight = 1.8;
    const aspect = width / height;
    camera.left = -halfHeight * aspect;
    camera.right = halfHeight * aspect;
    camera.top = halfHeight;
    camera.bottom = -halfHeight;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    renderer.setSize(width, height, false);
    render();
  }
  function follow(event: PointerEvent) {
    if (event.pointerType === "touch" || motion.matches) return;
    const rect = canvas.getBoundingClientRect();
    pointer.set(
      (event.clientX - rect.left) / rect.width - 0.5,
      (event.clientY - rect.top) / rect.height - 0.5,
    );
  }
  function reset() {
    pointer.set(0, 0);
  }
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(canvas);
  const intersection = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    syncMotion();
  });
  intersection.observe(canvas);
  canvas.addEventListener("pointermove", follow);
  canvas.addEventListener("pointerleave", reset);
  document.addEventListener("visibilitychange", syncMotion);
  motion.addEventListener("change", syncMotion);
  resize();
  syncMotion();
  return {
    dispose() {
      disposed = true;
      renderer.setAnimationLoop(null);
      resizeObserver.disconnect();
      intersection.disconnect();
      canvas.removeEventListener("pointermove", follow);
      canvas.removeEventListener("pointerleave", reset);
      document.removeEventListener("visibilitychange", syncMotion);
      motion.removeEventListener("change", syncMotion);
      const geometries = new Set<THREE.BufferGeometry>();
      const materials = new Set<THREE.Material>([graphite, silver, dark, blue, light]);
      scene.traverse((node) => {
        if (!(node instanceof THREE.Mesh)) return;
        geometries.add(node.geometry);
        for (const material of Array.isArray(node.material) ? node.material : [node.material])
          materials.add(material);
      });
      for (const geometry of geometries) geometry.dispose();
      for (const material of materials) material.dispose();
      environment.dispose();
      key.shadow.dispose();
      renderer.dispose();
    },
  };
}

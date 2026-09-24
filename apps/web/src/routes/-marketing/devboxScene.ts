import * as THREE from "three";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";

const agents = [
  { icon: "codex", position: new THREE.Vector3(-2.15, 0.1, 0.2) },
  { icon: "claude-code", position: new THREE.Vector3(0.15, 0.85, -2.15) },
  { icon: "opencode", position: new THREE.Vector3(2.5, 0.3, 0.3) },
];
const REVEAL_DURATION = 2600;

function ease(value: number) {
  return 1 - (1 - THREE.MathUtils.clamp(value, 0, 1)) ** 3;
}

function roundedSquare(size: number, y: number, radius = 0.16) {
  const half = size / 2;
  const path = new THREE.Shape();
  path.moveTo(-half + radius, -half);
  path.lineTo(half - radius, -half);
  path.quadraticCurveTo(half, -half, half, -half + radius);
  path.lineTo(half, half - radius);
  path.quadraticCurveTo(half, half, half - radius, half);
  path.lineTo(-half + radius, half);
  path.quadraticCurveTo(-half, half, -half, half - radius);
  path.lineTo(-half, -half + radius);
  path.quadraticCurveTo(-half, -half, -half + radius, -half);
  return new THREE.CatmullRomCurve3(
    path.getPoints(12).map((p) => new THREE.Vector3(p.x, y, p.y)),
    true,
    "centripetal",
  );
}

export async function createDevboxScene(
  canvas: HTMLCanvasElement,
  stage: HTMLDivElement,
  onContextLost: () => void,
) {
  const icons = await Promise.all(
    agents.map(async (agent) => {
      const image = new Image();
      image.src = `/agents/${agent.icon}.svg`;
      await image.decode();
      return image;
    }),
  );
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 1);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1;
  const scene = new THREE.Scene();
  const world = new THREE.Group();
  scene.add(world, new THREE.HemisphereLight("#d6eeff", "#080e16", 1.2));
  const key = new THREE.DirectionalLight("#e2f2ff", 4);
  key.position.set(-3, 7, 4);
  const rim = new THREE.DirectionalLight("#4faaff", 5);
  rim.position.set(4, 2, -5);
  scene.add(key, rim);
  const studio = new RoomEnvironment();
  const reflection = new THREE.PMREMGenerator(renderer);
  const environment = reflection.fromScene(studio, 0.04);
  scene.environment = environment.texture;
  scene.environmentIntensity = 0.65;
  studio.dispose();
  reflection.dispose();

  const camera = new THREE.OrthographicCamera(-5, 5, 4, -4, 0.1, 50);
  camera.position.set(6, 8, 12);
  camera.lookAt(0, 0, 0);
  const renderTarget = new THREE.WebGLRenderTarget(1, 1, {
    type: THREE.HalfFloatType,
    samples: 4,
  });
  const composer = new EffectComposer(renderer, renderTarget);
  const renderPass = new RenderPass(scene, camera);
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.55, 0.5, 1.1);
  const output = new OutputPass();
  composer.addPass(renderPass);
  composer.addPass(bloom);
  composer.addPass(output);

  const geometries: THREE.BufferGeometry[] = [];
  const materials: THREE.Material[] = [];
  const textures: THREE.Texture[] = [];
  const cache = new Map<string, THREE.BufferGeometry>();
  function metal(color: string, roughness = 0.28, metalness = 0.7) {
    const material = new THREE.MeshStandardMaterial({ color, roughness, metalness });
    materials.push(material);
    return material;
  }
  function light(color: string, intensity = 1) {
    const material = new THREE.MeshBasicMaterial({
      color: new THREE.Color(color).multiplyScalar(intensity),
    });
    materials.push(material);
    return material;
  }
  function box(
    width: number,
    height: number,
    depth: number,
    material: THREE.Material,
    radius = 0.06,
  ) {
    const id = `${width}/${height}/${depth}/${radius}`;
    let geometry = cache.get(id);
    if (!geometry) {
      geometry = new RoundedBoxGeometry(width, height, depth, 4, radius);
      cache.set(id, geometry);
      geometries.push(geometry);
    }
    return new THREE.Mesh(geometry, material);
  }
  function tube(path: THREE.Curve<THREE.Vector3>, radius: number, material: THREE.Material) {
    const geometry = new THREE.TubeGeometry(path, 100, radius, 6, false);
    geometries.push(geometry);
    return new THREE.Mesh(geometry, material);
  }
  const graphite = metal("#18212d", 0.31);
  const edge = metal("#526779", 0.26, 0.85);
  const inset = metal("#0b121b", 0.4);
  const glow = light("#50b7ff", 1.8);
  const trace = light("#285778", 0.8);
  const acrylic = new THREE.MeshPhysicalMaterial({
    color: "#1470aa",
    metalness: 0.25,
    roughness: 0.18,
    transparent: true,
    opacity: 0.65,
    clearcoat: 1,
    emissive: "#08477c",
    emissiveIntensity: 0.65,
  });
  materials.push(acrylic);
  const plane = new THREE.PlaneGeometry(1, 1);
  geometries.push(plane);

  // A single source fans out to isolated compute modules.
  const origin = new THREE.Vector3(-1.7, -0.85, 3.5);
  const source = new THREE.Group();
  source.position.copy(origin);
  const sourceBase = box(1.03, 0.16, 1.03, graphite);
  source.add(sourceBase, tube(roundedSquare(0.96, 0.09, 0.12), 0.013, glow));
  for (let index = 0; index < 3; index++) {
    const bar = box(0.32 - index * 0.07, 0.012, 0.035, light("#a5c8e0"), 0.005);
    bar.position.set(-0.03, 0.09, -0.13 + index * 0.13);
    source.add(bar);
  }
  world.add(source);

  const modules = agents.map((agent, index) => {
    const group = new THREE.Group();
    group.position.copy(agent.position);
    world.add(group);
    const base = box(2.02, 0.16, 2.02, edge);
    base.position.y = -0.2;
    const lower = box(1.98, 0.16, 1.98, graphite);
    lower.position.y = -0.11;
    const core = box(1.85, 0.29, 1.85, acrylic);
    core.position.y = 0.14;
    const coreLine = tube(roundedSquare(1.83, 0.15), 0.016, glow);
    const lid = new THREE.Group();
    lid.position.y = 0.49;
    const lip = box(2.04, 0.065, 2.04, edge, 0.03);
    const top = box(2, 0.2, 2, graphite);
    top.position.y = 0.09;
    const face = box(1.79, 0.025, 1.79, inset, 0.01);
    face.position.y = 0.2;
    const engraving = tube(roundedSquare(1.85, 0.215, 0.13), 0.006, light("#38516a"));
    lid.add(lip, top, face, engraving);
    group.add(base, lower, core, coreLine, lid);

    const bitmap = document.createElement("canvas");
    bitmap.width = 512;
    bitmap.height = 512;
    const context = bitmap.getContext("2d");
    if (!context) throw new Error("Agent icon canvas is unavailable");
    const ratio = icons[index].naturalWidth / icons[index].naturalHeight;
    const width = 400 * Math.min(1, ratio);
    const height = 400 / Math.max(1, ratio);
    context.drawImage(icons[index], (512 - width) / 2, (512 - height) / 2, width, height);
    if (agent.icon !== "opencode") {
      context.globalCompositeOperation = "source-in";
      context.fillStyle = "#e5eff7";
      context.fillRect(0, 0, 512, 512);
    }
    const texture = new THREE.CanvasTexture(bitmap);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = renderer.capabilities.getMaxAnisotropy();
    textures.push(texture);
    const iconMaterial = new THREE.MeshBasicMaterial({
      map: texture,
      transparent: true,
      depthWrite: false,
    });
    materials.push(iconMaterial);
    const icon = new THREE.Mesh(plane, iconMaterial);
    icon.rotation.x = -Math.PI / 2;
    icon.scale.setScalar(1.04);
    icon.position.y = 0.219;
    lid.add(icon);

    // Closely spaced conductors make one routed connection, with a shared source.
    const paths = [-0.065, 0, 0.065].map(
      (offset) =>
        new THREE.CubicBezierCurve3(
          new THREE.Vector3(origin.x + offset, origin.y, origin.z - 0.5),
          new THREE.Vector3(origin.x + offset, origin.y, 1.2),
          new THREE.Vector3(agent.position.x + offset, -0.85, agent.position.z + 1.7),
          new THREE.Vector3(agent.position.x + offset, agent.position.y - 0.3, agent.position.z),
        ),
    );
    const connections = paths.map((path) => {
      const mesh = tube(path, 0.009, trace);
      world.add(mesh);
      return mesh;
    });
    const signal = tube(paths[1], 0.016, glow);
    world.add(signal);
    return { group, lid, target: agent.position, connections, signal, index };
  });

  const gridPoints: number[] = [];
  for (let x = -4; x <= 4; x += 0.4) {
    for (let z = -3.6; z <= 4; z += 0.4) gridPoints.push(x, -1.15, z);
  }
  const gridGeometry = new THREE.BufferGeometry();
  gridGeometry.setAttribute("position", new THREE.Float32BufferAttribute(gridPoints, 3));
  geometries.push(gridGeometry);
  const gridMaterial = new THREE.PointsMaterial({
    color: "#34516a",
    size: 0.016,
    transparent: true,
    opacity: 0.38,
  });
  materials.push(gridMaterial);
  world.add(new THREE.Points(gridGeometry, gridMaterial));

  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const pointer = new THREE.Vector2();
  const look = new THREE.Vector2();
  let elapsed = motion.matches ? REVEAL_DURATION : 0;
  let visible = false;
  let disposed = false;
  let lost = false;
  let animation = 0;
  let previousTime = 0;

  function draw() {
    for (const module of modules) {
      const reveal = ease((elapsed - module.index * 180) / 1500);
      module.group.position.copy(module.target);
      module.group.position.y += (1 - reveal) * 0.6;
      module.lid.position.y = 0.49 + (1 - reveal) * 0.6;
      const route = ease((elapsed - 400 - module.index * 180) / 1400);
      for (const mesh of module.connections) {
        const count = mesh.geometry.index?.count ?? 0;
        mesh.geometry.setDrawRange(0, Math.floor((count * route) / 3) * 3);
      }
      const count = module.signal.geometry.index?.count ?? 0;
      const travel = (elapsed - 700 - module.index * 180) / 1000;
      module.signal.visible = !motion.matches && travel > 0 && travel < 1;
      module.signal.geometry.setDrawRange(
        Math.floor((count * Math.max(0, travel - 0.14)) / 3) * 3,
        Math.floor((count * 0.14) / 3) * 3,
      );
    }
    world.rotation.y = look.x * 0.1;
    world.rotation.x = look.y * 0.045;
    composer.render();
  }
  function tick(now: number) {
    animation = 0;
    if (disposed || lost || !visible || document.hidden) return;
    const delta = previousTime ? Math.min(now - previousTime, 100) : 0;
    previousTime = now;
    elapsed = Math.min(REVEAL_DURATION, elapsed + delta);
    look.lerp(pointer, 1 - Math.exp(-delta / 110));
    draw();
    if (!motion.matches && (elapsed < REVEAL_DURATION || look.distanceTo(pointer) > 0.001))
      animation = requestAnimationFrame(tick);
  }
  function requestDraw() {
    if (disposed || lost || animation || !visible || document.hidden) return;
    previousTime = 0;
    animation = requestAnimationFrame(tick);
  }
  function syncMotion() {
    cancelAnimationFrame(animation);
    animation = 0;
    if (motion.matches) {
      elapsed = REVEAL_DURATION;
      pointer.set(0, 0);
      look.set(0, 0);
    }
    requestDraw();
  }
  function resize() {
    if (disposed || lost) return;
    const { width, height } = stage.getBoundingClientRect();
    if (!width || !height) return;
    const aspect = width / height;
    const halfHeight = Math.max(3.55, 4.9 / aspect);
    camera.left = -halfHeight * aspect;
    camera.right = halfHeight * aspect;
    camera.top = halfHeight;
    camera.bottom = -halfHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    composer.setSize(width, height);
    requestDraw();
  }
  function follow(event: PointerEvent) {
    if (event.pointerType === "touch" || motion.matches) return;
    const bounds = stage.getBoundingClientRect();
    pointer.set(
      ((event.clientX - bounds.left) / bounds.width) * 2 - 1,
      ((event.clientY - bounds.top) / bounds.height) * 2 - 1,
    );
    requestDraw();
  }
  function resetPointer() {
    pointer.set(0, 0);
    requestDraw();
  }
  function contextLost(event: Event) {
    event.preventDefault();
    lost = true;
    cancelAnimationFrame(animation);
    animation = 0;
    onContextLost();
  }
  const intersection = new IntersectionObserver(
    ([entry]) => {
      visible = entry.isIntersecting;
      syncMotion();
    },
    { threshold: 0.15 },
  );
  const resizeObserver = new ResizeObserver(resize);
  intersection.observe(stage);
  resizeObserver.observe(stage);
  stage.addEventListener("pointermove", follow);
  stage.addEventListener("pointerleave", resetPointer);
  canvas.addEventListener("webglcontextlost", contextLost);
  document.addEventListener("visibilitychange", syncMotion);
  motion.addEventListener("change", syncMotion);
  resize();

  return {
    dispose() {
      disposed = true;
      cancelAnimationFrame(animation);
      intersection.disconnect();
      resizeObserver.disconnect();
      stage.removeEventListener("pointermove", follow);
      stage.removeEventListener("pointerleave", resetPointer);
      canvas.removeEventListener("webglcontextlost", contextLost);
      document.removeEventListener("visibilitychange", syncMotion);
      motion.removeEventListener("change", syncMotion);
      for (const geometry of geometries) geometry.dispose();
      for (const material of materials) material.dispose();
      for (const texture of textures) texture.dispose();
      environment.dispose();
      renderPass.dispose();
      bloom.dispose();
      output.dispose();
      composer.dispose();
      renderer.dispose();
    },
  };
}

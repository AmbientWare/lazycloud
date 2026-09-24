import * as THREE from "three";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";

const devboxAgents = [
  { name: "Codex", host: "codex-box", command: "codex" },
  { name: "Claude Code", host: "claude-box", command: "claude" },
  { name: "OpenCode", host: "opencode-box", command: "opencode" },
] as const;

const ASSEMBLY_DURATION = 3600;
const CONNECTION_DURATION = 900;

function ease(value: number) {
  return 1 - (1 - THREE.MathUtils.clamp(value, 0, 1)) ** 3;
}

export function createDevboxScene(
  canvas: HTMLCanvasElement,
  stage: HTMLDivElement,
  onContextLost: () => void,
) {
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  const studio = new RoomEnvironment();
  const reflection = new THREE.PMREMGenerator(renderer);
  const environment = reflection.fromScene(studio, 0.06);
  scene.environment = environment.texture;
  scene.environmentIntensity = 0.4;
  studio.dispose();
  reflection.dispose();
  const world = new THREE.Group();
  scene.add(world, new THREE.HemisphereLight("#dcebf4", "#151a24", 1.5));
  const key = new THREE.DirectionalLight("#e7f3fa", 3);
  key.position.set(-3, 7, 5);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.left = -7;
  key.shadow.camera.right = 7;
  key.shadow.camera.top = 7;
  key.shadow.camera.bottom = -7;
  key.shadow.normalBias = 0.025;
  const rim = new THREE.DirectionalLight("#76d6f5", 2.2);
  rim.position.set(4, 2, -5);
  scene.add(key, rim);

  const camera = new THREE.OrthographicCamera(-5, 5, 4, -4, 0.1, 60);
  camera.position.set(5.3, 6.7, 12);
  camera.lookAt(0, 0.2, 0);

  const geometries: THREE.BufferGeometry[] = [];
  const materials: THREE.Material[] = [];
  const textures: THREE.Texture[] = [];
  const cube = new RoundedBoxGeometry(1, 1, 1, 2, 0.025);
  const plane = new THREE.PlaneGeometry(1, 1);
  const edges = new THREE.EdgesGeometry(cube, 40);
  geometries.push(cube, plane, edges);

  function metal(color: string, roughness = 0.42) {
    const material = new THREE.MeshStandardMaterial({ color, roughness, metalness: 0.55 });
    materials.push(material);
    return material;
  }

  const shell = metal("#1c2630");
  const inset = metal("#0e1822", 0.75);
  const aluminium = metal("#516675", 0.4);
  const edgeMaterial = new THREE.LineBasicMaterial({
    color: "#7393a8",
    transparent: true,
    opacity: 0.18,
  });
  materials.push(edgeMaterial);

  function block(
    width: number,
    height: number,
    depth: number,
    material: THREE.Material,
    outlined = false,
  ) {
    const mesh = new THREE.Mesh(cube, material);
    mesh.scale.set(width, height, depth);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    if (outlined) mesh.add(new THREE.LineSegments(edges, edgeMaterial));
    return mesh;
  }

  function screen(title: string, lines: string[]) {
    const bitmap = document.createElement("canvas");
    bitmap.width = 1024;
    bitmap.height = 640;
    const context = bitmap.getContext("2d");
    if (!context) throw new Error("Dev box preview requires a 2D canvas for terminal labels.");
    const texture = new THREE.CanvasTexture(bitmap);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
    textures.push(texture);
    const material = new THREE.MeshBasicMaterial({ map: texture, toneMapped: false });
    materials.push(material);

    const write = (nextTitle: string, nextLines: string[]) => {
      context.fillStyle = "#0c151e";
      context.fillRect(0, 0, 1024, 640);
      context.fillStyle = "#e2edf4";
      context.font = '500 92px "Instrument Sans Variable", sans-serif';
      context.fillText(nextTitle, 65, 130);
      context.fillStyle = "#354b5a";
      context.fillRect(65, 185, 894, 2);
      context.font = '42px "JetBrains Mono Variable", monospace';
      nextLines.forEach((line, index) => {
        context.fillStyle = index === nextLines.length - 1 ? "#76d6f5" : "#a4b4c2";
        context.fillText(line, 65, 290 + index * 85);
      });
      texture.needsUpdate = true;
    };
    write(title, lines);
    return { material, write };
  }

  const workstation = new THREE.Group();
  const shadowMaterial = new THREE.ShadowMaterial({ opacity: 0.18 });
  materials.push(shadowMaterial);
  const ground = new THREE.Mesh(plane, shadowMaterial);
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.25;
  ground.scale.set(16, 16, 1);
  ground.receiveShadow = true;
  world.add(ground);
  workstation.position.set(0, 0, 2.4);
  world.add(workstation);
  const desk = block(2.7, 0.12, 1.65, shell, true);
  desk.position.y = -0.18;
  workstation.add(desk);
  const stand = block(0.16, 0.5, 0.15, aluminium);
  stand.position.set(0, 0.13, -0.45);
  workstation.add(stand);
  const monitor = block(2.3, 1.5, 0.12, shell, true);
  monitor.position.set(0, 1.06, -0.45);
  workstation.add(monitor);
  const terminal = screen("Your terminal", ["$ lazycloud ssh", devboxAgents[0].host]);
  const display = new THREE.Mesh(plane, terminal.material);
  display.scale.set(2.1, 1.3, 1);
  display.position.set(0, 1.06, -0.378);
  workstation.add(display);
  const keyboard = block(1.6, 0.045, 0.5, inset, true);
  keyboard.position.set(0, -0.087, 0.38);
  workstation.add(keyboard);
  for (let row = 0; row < 3; row++) {
    for (let col = 0; col < 12; col++) {
      const keycap = block(0.09, 0.015, 0.075, aluminium);
      keycap.position.set(-0.66 + col * 0.12, -0.056, 0.24 + row * 0.12);
      workstation.add(keycap);
    }
  }

  const destinations = [
    new THREE.Vector3(-2.8, 0, -0.2),
    new THREE.Vector3(-0.35, 0, -2.6),
    new THREE.Vector3(2.65, 0, -1.35),
  ];

  const machines = devboxAgents.map((agent, index) => {
    const group = new THREE.Group();
    world.add(group);
    const base = block(1.92, 0.16, 1.64, shell, true);
    base.position.y = -0.08;
    group.add(base);
    const disk = block(1.58, 0.075, 1.32, aluminium, true);
    disk.position.y = 0.07;
    group.add(disk);
    const body = block(1.76, 1.12, 1.48, shell, true);
    body.position.y = 0.72;
    group.add(body);
    const label = screen(agent.name, ["~/project", `$ ${agent.command}`]);
    const face = new THREE.Mesh(plane, label.material);
    face.scale.set(1.58, 0.98, 1);
    face.position.set(0, 0.72, 0.747);
    group.add(face);

    const lid = new THREE.Group();
    const cover = block(1.92, 0.075, 1.64, shell, true);
    lid.add(cover);
    for (let slot = 0; slot < 7; slot++) {
      const vent = block(0.055, 0.016, 0.85, inset);
      vent.position.set(-0.48 + slot * 0.16, 0.045, 0);
      lid.add(vent);
    }
    group.add(lid);
    const accent = metal("#69c5eb", 0.35);
    accent.emissive.set("#3484b0");
    const seam = block(1.77, 0.025, 1.49, accent);
    seam.position.y = 0.15;
    group.add(seam);

    const target = destinations[index];
    const curve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(index === 0 ? -1.15 : 1.15, -0.18, 2.3),
      new THREE.Vector3(index === 0 ? -1.8 : 1.9 + index * 0.3, -0.18, 1.2),
      new THREE.Vector3(target.x + 0.75, -0.18, target.z + 1.25),
      new THREE.Vector3(target.x, -0.05, target.z + 0.8),
    ]);
    const cableGeometry = new THREE.TubeGeometry(curve, 64, 0.016, 6, false);
    geometries.push(cableGeometry);
    const cableMaterial = new THREE.MeshBasicMaterial({ color: "#29465a", transparent: true });
    const packetMaterial = new THREE.MeshBasicMaterial({ color: "#b2ecff" });
    materials.push(cableMaterial, packetMaterial);
    const cable = new THREE.Mesh(cableGeometry, cableMaterial);
    world.add(cable);
    const packet = block(0.08, 0.06, 0.15, packetMaterial);
    world.add(packet);
    return { group, target, body, face, lid, accent, cableMaterial, packet, curve };
  });

  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const pointer = new THREE.Vector2();
  const look = new THREE.Vector2();
  const raycaster = new THREE.Raycaster();
  const hitPoint = new THREE.Vector2();
  let selected = 0;
  let elapsed = motion.matches ? ASSEMBLY_DURATION : 0;
  let packetAge = CONNECTION_DURATION;
  let visible = false;
  let disposed = false;
  let lost = false;
  let frame = 0;
  let previousTime = 0;

  function draw() {
    const assembling = elapsed < ASSEMBLY_DURATION;
    for (const [index, machine] of machines.entries()) {
      const spread = ease((elapsed - 350 - index * 180) / 1400);
      const build = ease((elapsed - 1000 - index * 180) / 1000);
      const close = ease((elapsed - 1800 - index * 180) / 700);
      machine.group.position.set(
        THREE.MathUtils.lerp(-0.35, machine.target.x, spread),
        (1 - spread) * index * 0.22,
        THREE.MathUtils.lerp(-1.1, machine.target.z, spread),
      );
      machine.group.scale.setScalar(0.7 + spread * 0.3);
      machine.body.scale.y = 1.12 * Math.max(0.015, build);
      machine.body.position.y = 0.16 + build * 0.56;
      machine.face.scale.y = 0.98 * Math.max(0.015, build);
      machine.face.position.y = machine.body.position.y;
      machine.lid.position.y = 0.22 + build * 1.9 - close * 0.78;
      machine.accent.emissiveIntensity = index === selected ? 1.6 : 0.2;
      machine.cableMaterial.color.set(index === selected ? "#69bfe0" : "#29465a");
      machine.cableMaterial.opacity = ease((elapsed - 2000) / 600);
      const travel = assembling
        ? (elapsed - 2300 - index * 150) / 700
        : packetAge / CONNECTION_DURATION;
      machine.packet.visible =
        !motion.matches && travel > 0 && travel < 1 && (assembling || index === selected);
      if (machine.packet.visible) {
        machine.packet.position.copy(machine.curve.getPointAt(travel));
        const tangent = machine.curve.getTangentAt(travel);
        machine.packet.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), tangent);
      }
    }
    world.rotation.y = look.x * 0.12;
    world.rotation.x = look.y * 0.045;
    renderer.render(scene, camera);
  }

  function tick(now: number) {
    frame = 0;
    if (disposed || lost || !visible || document.hidden) return;
    const delta = previousTime ? Math.min(now - previousTime, 50) : 0;
    previousTime = now;
    elapsed = Math.min(ASSEMBLY_DURATION, elapsed + delta);
    packetAge = Math.min(CONNECTION_DURATION, packetAge + delta);
    look.lerp(pointer, 1 - Math.exp(-delta / 110));
    draw();
    if (
      !motion.matches &&
      (elapsed < ASSEMBLY_DURATION ||
        packetAge < CONNECTION_DURATION ||
        look.distanceTo(pointer) > 0.001)
    ) {
      frame = requestAnimationFrame(tick);
    }
  }

  function requestDraw() {
    if (disposed || lost || frame || !visible || document.hidden) return;
    previousTime = 0;
    frame = requestAnimationFrame(tick);
  }

  function syncMotion() {
    cancelAnimationFrame(frame);
    frame = 0;
    if (motion.matches) {
      elapsed = ASSEMBLY_DURATION;
      packetAge = CONNECTION_DURATION;
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
    const halfHeight = Math.max(3.3, 4.4 / aspect);
    camera.left = -halfHeight * aspect;
    camera.right = halfHeight * aspect;
    camera.top = halfHeight;
    camera.bottom = -halfHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    requestDraw();
  }

  function follow(event: PointerEvent) {
    if (event.pointerType === "touch") return;
    const bounds = stage.getBoundingClientRect();
    hitPoint.set(
      ((event.clientX - bounds.left) / bounds.width) * 2 - 1,
      1 - ((event.clientY - bounds.top) / bounds.height) * 2,
    );
    if (!motion.matches) pointer.set(hitPoint.x, -hitPoint.y);
    raycaster.setFromCamera(hitPoint, camera);
    const hit = raycaster.intersectObjects(
      machines.map((machine) => machine.body),
      false,
    )[0];
    const index = machines.findIndex((machine) => machine.body === hit?.object);
    if (index >= 0 && index !== selected) {
      selected = index;
      terminal.write("Your terminal", ["$ lazycloud ssh", devboxAgents[index].host]);
      packetAge = motion.matches ? CONNECTION_DURATION : 0;
    }
    requestDraw();
  }

  function resetPointer() {
    pointer.set(0, 0);
    requestDraw();
  }

  function contextLost(event: Event) {
    event.preventDefault();
    lost = true;
    cancelAnimationFrame(frame);
    frame = 0;
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
      cancelAnimationFrame(frame);
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
      key.shadow.dispose();
      renderer.dispose();
    },
  };
}

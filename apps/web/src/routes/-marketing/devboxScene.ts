import * as THREE from "three";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";

const AGENTS = ["codex", "claude-code", "opencode"];
const ASSEMBLED_COUNT = 27;
const COUNT = ASSEMBLED_COUNT + 36;
const EXPANSION_DURATION = 800;
const foregroundRotations = [
  new THREE.Euler(-0.18, 0.5, -0.14),
  new THREE.Euler(0.1, 0.05, 0.16),
  new THREE.Euler(-0.12, 0.85, 0.08),
];

function ease(value: number) {
  const t = THREE.MathUtils.clamp(value, 0, 1);
  return t * t * t * (t * (t * 6 - 15) + 10);
}

function spring(seconds: number) {
  const damping = 0.68;
  const frequency = 22;
  const damped = frequency * Math.sqrt(1 - damping * damping);
  return (
    1 -
    Math.exp(-damping * frequency * seconds) *
      (Math.cos(damped * seconds) + ((damping * frequency) / damped) * Math.sin(damped * seconds))
  );
}

export async function createDevboxScene(
  canvas: HTMLCanvasElement,
  stage: HTMLButtonElement,
  onContextLost: () => void,
) {
  const grainSource = getComputedStyle(stage).getPropertyValue("--surface-grain").trim();
  const grainUrl = /^url\(["']?(.*?)["']?\)$/.exec(grainSource)?.[1];
  if (!grainUrl) throw new Error("The surface grain texture is unavailable");
  const images = await Promise.all(
    [...AGENTS.map((agent) => `/agents/${agent}.svg`), "/lazycloud.png", grainUrl].map(
      async (url) => {
        const image = new Image();
        image.src = url;
        await image.decode();
        return image;
      },
    ),
  );
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  const scene = new THREE.Scene();
  const world = new THREE.Group();
  scene.add(world, new THREE.HemisphereLight("#e0e5e9", "#111519", 1.2));
  const key = new THREE.DirectionalLight("#e2e8ed", 2);
  key.position.set(-3, 7, 4);
  const rim = new THREE.DirectionalLight("#7cabc4", 1.2);
  rim.position.set(4, 2, -5);
  scene.add(key, rim);
  const studio = new RoomEnvironment();
  const reflection = new THREE.PMREMGenerator(renderer);
  const environment = reflection.fromScene(studio, 0.04);
  scene.environment = environment.texture;
  scene.environmentIntensity = 0.35;
  studio.dispose();
  reflection.dispose();

  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 60);
  const cameraDirection = new THREE.Vector3(6, 5, 12).normalize();
  camera.position.copy(cameraDirection).multiplyScalar(15);
  camera.lookAt(0, 0, 0);
  const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
  const up = new THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion);
  const textures: THREE.Texture[] = [];
  const materials: THREE.Material[] = [];
  const geometries: THREE.BufferGeometry[] = [];

  const grainBitmap = document.createElement("canvas");
  grainBitmap.width = grainBitmap.height = 360;
  const grainContext = grainBitmap.getContext("2d");
  if (!grainContext) throw new Error("The surface texture canvas is unavailable");
  grainContext.fillStyle = "#bdbdbd";
  grainContext.fillRect(0, 0, 360, 360);
  grainContext.drawImage(images[4], 0, 0, 360, 360);
  const grain = new THREE.CanvasTexture(grainBitmap);
  grain.colorSpace = THREE.SRGBColorSpace;
  grain.anisotropy = renderer.capabilities.getMaxAnisotropy();
  textures.push(grain);

  function surface(color: string) {
    const material = new THREE.MeshStandardMaterial({
      color,
      roughness: 0.78,
      metalness: 0.12,
      map: grain,
      bumpMap: grain,
      bumpScale: 0.016,
    });
    materials.push(material);
    return material;
  }
  const bodyMaterial = surface("#30383e");
  const capMaterial = surface("#20272c");
  const seamMaterial = new THREE.MeshBasicMaterial({ color: "#527f96" });
  materials.push(seamMaterial);
  const bodyGeometry = new RoundedBoxGeometry(0.94, 0.94, 0.94, 4, 0.055);
  const capGeometry = new RoundedBoxGeometry(0.86, 0.024, 0.86, 3, 0.01);
  const seamGeometry = new RoundedBoxGeometry(0.9, 0.012, 0.9, 3, 0.005);
  const plane = new THREE.PlaneGeometry(1, 1);
  geometries.push(bodyGeometry, capGeometry, seamGeometry, plane);
  const bodies = new THREE.InstancedMesh(bodyGeometry, bodyMaterial, COUNT);
  const caps = new THREE.InstancedMesh(capGeometry, capMaterial, COUNT);
  const seams = new THREE.InstancedMesh(seamGeometry, seamMaterial, COUNT);
  const instances: THREE.InstancedMesh[] = [bodies, caps, seams];
  world.add(...instances);

  const agentMarks = AGENTS.map((agent, index) => {
    const bitmap = document.createElement("canvas");
    bitmap.width = bitmap.height = 256;
    const context = bitmap.getContext("2d");
    if (!context) throw new Error("Agent icon canvas is unavailable");
    const image = images[index];
    const ratio = image.naturalWidth / image.naturalHeight;
    const width = 220 * Math.min(1, ratio);
    const height = 220 / Math.max(1, ratio);
    context.drawImage(image, (256 - width) / 2, (256 - height) / 2, width, height);
    if (agent !== "opencode") {
      context.globalCompositeOperation = "source-in";
      context.fillStyle = "#d0dce2";
      context.fillRect(0, 0, 256, 256);
    }
    const texture = new THREE.CanvasTexture(bitmap);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = renderer.capabilities.getMaxAnisotropy();
    textures.push(texture);
    const material = new THREE.MeshBasicMaterial({
      map: texture,
      transparent: true,
      depthWrite: false,
    });
    materials.push(material);
    const mesh = new THREE.InstancedMesh(plane, material, COUNT / AGENTS.length);
    instances.push(mesh);
    world.add(mesh);
    return { mesh, material };
  });

  const shellGeometry = new RoundedBoxGeometry(2.87, 2.87, 2.87, 5, 0.09);
  geometries.push(shellGeometry);
  const shellMaterial = surface("#30383e");
  shellMaterial.transparent = true;
  const shell = new THREE.Mesh(shellGeometry, shellMaterial);
  world.add(shell);
  const sharedTexture = new THREE.Texture(images[3]);
  sharedTexture.colorSpace = THREE.SRGBColorSpace;
  sharedTexture.needsUpdate = true;
  textures.push(sharedTexture);
  const sharedMaterial = new THREE.MeshBasicMaterial({
    map: sharedTexture,
    transparent: true,
    depthWrite: false,
  });
  materials.push(sharedMaterial);
  const sharedIcon = new THREE.Mesh(plane, sharedMaterial);
  sharedIcon.position.set(0, 0, 1.44);
  sharedIcon.scale.setScalar(1.45);
  shell.add(sharedIcon);

  const cubes = Array.from({ length: COUNT }, (_, index) => {
    const background = index >= ASSEMBLED_COUNT;
    const packed = new THREE.Vector3(
      (index % 3) - 1,
      (Math.floor(index / 3) % 3) - 1,
      Math.floor(index / 9) - 1,
    ).multiplyScalar(0.96);
    const size = index < 3 ? 1.4 : 0.65 + (index % 5) * 0.11;
    const rotation =
      index < 3
        ? foregroundRotations[index]
        : new THREE.Euler(
            Math.sin(index * 2.7) * 0.45,
            0.45 + Math.cos(index * 2.4) * 1.05,
            Math.sin(index * 1.7) * 0.4,
          );
    const shade = new THREE.Color().setScalar(background ? 0.65 : 1);
    bodies.setColorAt(index, shade);
    caps.setColorAt(index, shade);
    seams.setColorAt(index, shade);
    agentMarks[index % AGENTS.length].mesh.setColorAt(Math.floor(index / AGENTS.length), shade);
    return {
      packed,
      origin: packed.clone(),
      target: new THREE.Vector3(),
      size,
      rotation,
      background,
    };
  });
  const transform = new THREE.Object3D();
  const local = new THREE.Matrix4();
  const matrix = new THREE.Matrix4();
  const unitQuaternion = new THREE.Quaternion();
  const capOffset = new THREE.Vector3(0, 0.49, 0);
  const seamOffset = new THREE.Vector3(0, 0.475, 0);
  const iconOffset = new THREE.Vector3(0, 0, 0.475);
  const unitScale = new THREE.Vector3(1, 1, 1);
  const iconScale = new THREE.Vector3(0.7, 0.7, 0.7);
  for (const mesh of instances) {
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    // The instances move beyond their initial bounds during the split.
    mesh.frustumCulled = false;
  }

  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const pointer = new THREE.Vector2();
  const look = new THREE.Vector2();
  let expansion = motion.matches ? 1 : 0;
  let fromExpansion = 0;
  let targetExpansion = 1;
  let transitionElapsed = motion.matches ? EXPANSION_DURATION : -350;
  let transitionDuration = EXPANSION_DURATION;
  let visible = false;
  let disposed = false;
  let lost = false;
  let animation = 0;
  let previousTime = 0;

  function draw() {
    shellMaterial.opacity = 1 - ease(expansion / 0.15);
    sharedMaterial.opacity = shellMaterial.opacity;
    shell.visible = shellMaterial.opacity > 0;
    for (const mesh of instances) mesh.visible = expansion > 0.04;
    for (const mark of agentMarks) mark.material.opacity = ease((expansion - 0.15) / 0.55);
    cubes.forEach((cube, index) => {
      const spread = expansion;
      const turn =
        Math.sin(Math.PI * THREE.MathUtils.clamp(transitionElapsed / transitionDuration, 0, 1)) *
        Math.abs(targetExpansion - fromExpansion);
      transform.position.lerpVectors(cube.origin, cube.target, spread);
      transform.rotation.set(
        cube.rotation.x * spread + turn * ((index % 3) - 1) * 0.12,
        cube.rotation.y * spread + turn * (index % 2 ? 0.4 : -0.4),
        cube.rotation.z * spread + turn * ((index % 3) - 1) * 0.08,
      );
      transform.scale.setScalar(
        cube.background
          ? cube.size * ease((spread - 0.08) / 0.92)
          : THREE.MathUtils.lerp(1, cube.size, spread),
      );
      transform.updateMatrix();
      bodies.setMatrixAt(index, transform.matrix);
      local.compose(capOffset, unitQuaternion, unitScale);
      matrix.multiplyMatrices(transform.matrix, local);
      caps.setMatrixAt(index, matrix);
      local.compose(seamOffset, unitQuaternion, unitScale);
      matrix.multiplyMatrices(transform.matrix, local);
      seams.setMatrixAt(index, matrix);
      local.compose(iconOffset, unitQuaternion, iconScale);
      matrix.multiplyMatrices(transform.matrix, local);
      agentMarks[index % AGENTS.length].mesh.setMatrixAt(Math.floor(index / AGENTS.length), matrix);
    });
    for (const mesh of instances) mesh.instanceMatrix.needsUpdate = true;
    world.rotation.y = look.x * 0.1 - 0.12 * (1 - expansion);
    world.rotation.x = look.y * 0.045;
    renderer.render(scene, camera);
  }
  function tick(now: number) {
    animation = 0;
    if (disposed || lost || !visible || document.hidden) return;
    const delta = previousTime ? Math.min(now - previousTime, 100) : 0;
    previousTime = now;
    transitionElapsed = Math.min(transitionDuration, transitionElapsed + delta);
    const progress =
      transitionElapsed >= transitionDuration
        ? 1
        : spring((Math.max(0, transitionElapsed) / 1000) * (500 / transitionDuration));
    expansion = THREE.MathUtils.lerp(fromExpansion, targetExpansion, progress);
    look.lerp(pointer, 1 - Math.exp(-delta / 110));
    draw();
    if (
      !motion.matches &&
      (transitionElapsed < transitionDuration || look.distanceTo(pointer) > 0.001)
    )
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
      expansion = targetExpansion;
      transitionElapsed = transitionDuration;
      pointer.set(0, 0);
      look.set(0, 0);
    }
    requestDraw();
  }
  function resize() {
    if (disposed || lost) return;
    const { width, height } = stage.getBoundingClientRect();
    if (!width || !height) return;
    camera.aspect = width / height;
    const distance = Math.max(13.5, 16.8 / camera.aspect);
    camera.position.copy(cameraDirection).multiplyScalar(distance);
    camera.updateProjectionMatrix();
    const mobile = width < 1024;
    const halfHeight = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * distance;
    const halfWidth = halfHeight * camera.aspect;
    const focus = right
      .clone()
      .multiplyScalar(mobile ? 0 : -halfWidth * 0.46)
      .addScaledVector(up, mobile ? -halfHeight * 0.57 : 0);
    shell.position.copy(focus);
    cubes.forEach((cube, index) => {
      let x: number;
      let y: number;
      if (cube.background) {
        const slot = index - ASSEMBLED_COUNT;
        x = ((slot % 6) - 2.5) * 0.43 + Math.sin(index * 3.1) * 0.13;
        y = (Math.floor(slot / 6) - 2.5) * 0.38 + Math.cos(index * 1.7) * 0.1;
      } else if (index < 3) {
        x = mobile ? [0, -0.5, 0.48][index] : [-0.48, -0.68, -0.24][index];
        y = mobile ? [-0.34, -0.68, -0.73][index] : [0.27, -0.27, -0.34][index];
      } else {
        const slot = index - 3 >= 12 ? index - 2 : index - 3;
        x = ((slot % 5) - 2) * 0.51 + Math.sin(index * 4.7) * 0.09;
        y = (Math.floor(slot / 5) - 2) * 0.49 + Math.cos(index * 2.3) * 0.08;
      }
      const depth = cube.background
        ? -4 - (index % 5) * 1.2
        : index < 3
          ? 1
          : -1 - (index % 4) * 0.8;
      const depthScale = (distance - depth) / distance;
      cube.origin.copy(cube.packed).add(focus);
      cube.target
        .copy(right)
        .multiplyScalar(x * halfWidth * depthScale)
        .addScaledVector(up, y * halfHeight * depthScale)
        .addScaledVector(cameraDirection, depth);
      if (cube.background) cube.origin.copy(cube.target);
      cube.size = cube.background
        ? 0.6 + (index % 4) * 0.1
        : index < 3 && mobile
          ? 0.98
          : index < 3
            ? 1.4
            : 0.65 + (index % 5) * 0.11;
    });
    renderer.setSize(width, height, false);
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
  function toggleExpansion() {
    fromExpansion = expansion;
    targetExpansion = targetExpansion === 1 ? 0 : 1;
    transitionDuration = targetExpansion === 1 ? EXPANSION_DURATION : 300;
    transitionElapsed = motion.matches ? transitionDuration : 0;
    stage.setAttribute("aria-pressed", String(targetExpansion === 1));
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
  stage.setAttribute("aria-pressed", "true");
  stage.addEventListener("click", toggleExpansion);
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
      stage.removeEventListener("click", toggleExpansion);
      canvas.removeEventListener("webglcontextlost", contextLost);
      document.removeEventListener("visibilitychange", syncMotion);
      motion.removeEventListener("change", syncMotion);
      for (const mesh of instances) mesh.dispose();
      for (const geometry of geometries) geometry.dispose();
      for (const material of materials) material.dispose();
      for (const texture of textures) texture.dispose();
      environment.dispose();
      renderer.dispose();
    },
  };
}

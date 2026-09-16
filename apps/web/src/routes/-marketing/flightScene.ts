import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { createShip } from "./shipModel";
import { createLaunchPlatform } from "./launchPlatform";
import { createStarfield } from "./starfield";

export function createFlightScene(canvas: HTMLCanvasElement) {
  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: true,
    powerPreference: "low-power",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color("#000000");
  const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 50);
  const environment = new THREE.Scene();
  environment.background = new THREE.Color("#243240");
  const lightCards: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>[] = [];
  for (const [x, y, z, width, height, color, intensity] of [
    [-4, 4, 6, 3, 7, "#e4eeff", 5],
    [-5, -3, 5, 4, 9, "#e2edff", 4],
    [4, 1, 2, 0.8, 6, "#72baff", 4],
    [0, 6, -1, 4, 2, "#ffffff", 3],
    [0, -4, 3, 3, 1, "#99cfff", 1],
  ] as const) {
    const card = new THREE.Mesh(
      new THREE.PlaneGeometry(width, height),
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(color).multiplyScalar(intensity),
        side: THREE.DoubleSide,
      }),
    );
    card.position.set(x, y, z);
    card.lookAt(0, 0, 0);
    environment.add(card);
    lightCards.push(card);
  }
  const pmrem = new THREE.PMREMGenerator(renderer);
  const environmentMap = pmrem.fromScene(environment, 0.04);
  scene.environment = environmentMap.texture;
  scene.environmentIntensity = 1;
  for (const card of lightCards) {
    card.geometry.dispose();
    card.material.dispose();
  }
  pmrem.dispose();
  scene.add(new THREE.HemisphereLight("#b8d9ef", "#081019", 0.5));
  const key = new THREE.DirectionalLight("#fff4e8", 2.2);
  key.position.set(-3, 5, 4);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.left = key.shadow.camera.bottom = -5;
  key.shadow.camera.right = key.shadow.camera.top = 5;
  key.shadow.normalBias = 0.018;
  key.shadow.bias = -0.0001;
  const rim = new THREE.DirectionalLight("#4aaaff", 4);
  rim.position.set(4, 1, -2);
  scene.add(key, rim);
  const { ship, parts, exhaust, exhaustMaterial, engineLight } = createShip();
  const { platform, clamps, wash } = createLaunchPlatform();
  const { stars, update: updateStars } = createStarfield();
  scene.add(ship, platform, stars);
  const launchRotation = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, -0.35, 0));
  const flightRotation = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.24, -0.44, -0.62));
  const composer = new EffectComposer(renderer);
  const renderPass = new RenderPass(scene, camera);
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.3, 0.35, 2.8);
  const output = new OutputPass();
  composer.addPass(renderPass);
  composer.addPass(bloom);
  composer.addPass(output);

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let elapsed = reducedMotion.matches ? 3.4 : 0;
  let previousTime = 0;
  let visible = true;
  let cameraDistance = 11.8;
  const pointer = new THREE.Vector2();
  const viewOffset = new THREE.Vector2();

  function render() {
    const ignition = THREE.MathUtils.smoothstep(elapsed, 1.05, 1.35);
    const lift = THREE.MathUtils.smoothstep(elapsed, 1.35, 2.5);
    const flight = THREE.MathUtils.smoothstep(elapsed, 1.8, 3.2);
    for (const part of parts) {
      const remaining = 1 - THREE.MathUtils.smoothstep(elapsed, part.delay, part.delay + 0.44);
      part.group.position.copy(part.offset).multiplyScalar(remaining);
      part.group.rotation.set(
        part.spin.x * remaining,
        part.spin.y * remaining,
        part.spin.z * remaining,
      );
    }
    ship.quaternion.copy(launchRotation).slerp(flightRotation, flight);
    const cruiseTime = Math.max(0, elapsed - 3.2);
    ship.position.set(flight * 0.35, lift * 0.45 + Math.sin(cruiseTime * 1.1) * 0.055, 0);
    ship.scale.setScalar(1.05 + flight * 0.17);
    exhaust.visible = ignition > 0;
    exhaust.scale.y = ignition * (0.18 + lift * 0.9 + flight * 0.25);
    exhaustMaterial.uniforms.time.value = elapsed;
    engineLight.intensity = ignition * (0.4 + flight * 0.8);
    platform.position.set(-flight * 2, -1.9 - lift * 7, -lift * 3);
    platform.scale.setScalar(0.78 - lift * 0.3);
    platform.visible = lift < 1;
    const release = THREE.MathUtils.smoothstep(elapsed, 1.1, 1.4);
    clamps.forEach((clamp, index) => {
      const angle = (index / 3) * Math.PI * 2;
      clamp.position.set(Math.sin(angle) * release * 0.5, 0, Math.cos(angle) * release * 0.5);
    });
    wash.material.uniforms.strength.value = ignition * (1 - lift);
    updateStars(elapsed, flight, reducedMotion.matches);
    if (reducedMotion.matches) viewOffset.set(0, 0);
    else viewOffset.lerp(pointer, 0.035);
    camera.position.set(
      viewOffset.x * 0.5,
      2.1 * (1 - flight) + viewOffset.y * 0.35,
      cameraDistance,
    );
    camera.lookAt(0, 0, 0);
    composer.render();
  }
  function animate(now: number) {
    if (previousTime) elapsed += (now - previousTime) / 1000;
    previousTime = now;
    render();
  }
  function syncMotion() {
    previousTime = 0;
    renderer.setAnimationLoop(
      visible && !document.hidden && !reducedMotion.matches ? animate : null,
    );
    render();
  }
  function resize() {
    const { width, height } = canvas.getBoundingClientRect();
    if (!width || !height) return;
    camera.aspect = width / height;
    cameraDistance = Math.max(11.8, 9.5 / camera.aspect);
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    composer.setSize(width, height);
    render();
  }
  function movePointer(event: PointerEvent) {
    const rect = canvas.getBoundingClientRect();
    pointer.set(
      (event.clientX - rect.left) / rect.width - 0.5,
      (event.clientY - rect.top) / rect.height - 0.5,
    );
  }
  function resetPointer() {
    pointer.set(0, 0);
  }
  function motionPreference() {
    if (reducedMotion.matches) elapsed = Math.max(elapsed, 3.4);
    syncMotion();
  }
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(canvas);
  const intersection = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    syncMotion();
  });
  intersection.observe(canvas);
  canvas.addEventListener("pointermove", movePointer);
  canvas.addEventListener("pointerleave", resetPointer);
  document.addEventListener("visibilitychange", syncMotion);
  reducedMotion.addEventListener("change", motionPreference);
  resize();
  syncMotion();

  return {
    dispose() {
      renderer.setAnimationLoop(null);
      resizeObserver.disconnect();
      intersection.disconnect();
      canvas.removeEventListener("pointermove", movePointer);
      canvas.removeEventListener("pointerleave", resetPointer);
      document.removeEventListener("visibilitychange", syncMotion);
      reducedMotion.removeEventListener("change", motionPreference);
      const geometries = new Set<THREE.BufferGeometry>();
      const materials = new Set<THREE.Material>();
      scene.traverse((node) => {
        if (node instanceof THREE.InstancedMesh) node.dispose();
        if (
          node instanceof THREE.Mesh ||
          node instanceof THREE.Line ||
          node instanceof THREE.Points
        ) {
          geometries.add(node.geometry);
          for (const material of Array.isArray(node.material) ? node.material : [node.material])
            materials.add(material);
        }
      });
      for (const geometry of geometries) geometry.dispose();
      for (const material of materials) material.dispose();
      environmentMap.dispose();
      key.shadow.dispose();
      bloom.dispose();
      output.dispose();
      renderPass.dispose();
      composer.dispose();
      renderer.dispose();
    },
  };
}

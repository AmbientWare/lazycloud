import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

export function createHeroScene(
  canvas: HTMLCanvasElement,
  stage: HTMLDivElement,
  createContent: (scene: THREE.Scene) => {
    update: (elapsed: number, reducedMotion: boolean) => void;
  },
) {
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
  const content = createContent(scene);
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
    content.update(elapsed, reducedMotion.matches);
    const flight = THREE.MathUtils.smoothstep(elapsed, 1.8, 3.2);
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
    const { width, height, left, top } = canvas.getBoundingClientRect();
    const frame = stage.getBoundingClientRect();
    if (!width || !height) return;
    cameraDistance = Math.max(11.8, 9.5 / (frame.width / frame.height));
    camera.setViewOffset(
      frame.width,
      frame.height,
      left - frame.left,
      top - frame.top,
      width,
      height,
    );
    renderer.setSize(width, height, false);
    composer.setSize(width, height);
    render();
  }
  function movePointer(event: PointerEvent) {
    const rect = stage.getBoundingClientRect();
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
  resizeObserver.observe(stage);
  const intersection = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    syncMotion();
  });
  intersection.observe(canvas);
  stage.addEventListener("pointermove", movePointer);
  stage.addEventListener("pointerleave", resetPointer);
  document.addEventListener("visibilitychange", syncMotion);
  reducedMotion.addEventListener("change", motionPreference);
  resize();
  syncMotion();

  return {
    dispose() {
      renderer.setAnimationLoop(null);
      resizeObserver.disconnect();
      intersection.disconnect();
      stage.removeEventListener("pointermove", movePointer);
      stage.removeEventListener("pointerleave", resetPointer);
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

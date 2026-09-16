import * as THREE from "three";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { createShip } from "./shipModel";
import { createLaunchPlatform } from "./launchPlatform";
import { createAbstractEarth } from "./abstractEarth";

export type FlightPhase = "Assembling" | "Ignition" | "Liftoff" | "In orbit";

const ease = (value: number) => {
  const t = THREE.MathUtils.clamp(value, 0, 1);
  return t * t * (3 - 2 * t);
};

export function createFlightScene(
  canvas: HTMLCanvasElement,
  onPhase: (phase: FlightPhase) => void,
) {
  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: true,
    powerPreference: "low-power",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.1;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 100);
  camera.position.set(0, 3.2, 12.8);
  const environment = new RoomEnvironment();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const environmentMap = pmrem.fromScene(environment, 0.04);
  scene.environment = environmentMap.texture;
  environment.dispose();
  pmrem.dispose();
  scene.add(new THREE.HemisphereLight("#b5e8ff", "#102033", 1.5));
  const key = new THREE.DirectionalLight("#e9f4ff", 3.5);
  key.position.set(-4, 6, 5);
  scene.add(key);
  const rim = new THREE.DirectionalLight("#55bbff", 3);
  rim.position.set(4, 1, -3);
  scene.add(rim);

  const { ship, parts, exhaust } = createShip();
  scene.add(ship);
  const { platform, clamps, wash } = createLaunchPlatform();
  scene.add(platform);
  const { earth, texture } = createAbstractEarth();
  scene.add(earth);

  const orbit = new THREE.Group();
  orbit.rotation.set(0.9, 0.15, -0.3);
  const orbitPoints = Array.from({ length: 161 }, (_, i) => {
    const angle = (i / 160) * Math.PI * 2;
    return new THREE.Vector3(Math.cos(angle) * 3.4, Math.sin(angle) * 3.4, 0);
  });
  const orbitMaterial = new THREE.LineBasicMaterial({
    color: "#70b6d1",
    transparent: true,
    opacity: 0,
  });
  orbit.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(orbitPoints), orbitMaterial));
  scene.add(orbit);

  const starsGeometry = new THREE.BufferGeometry();
  const starPositions = new Float32Array(90 * 3);
  let seed = 29;
  const random = () => {
    seed = (seed * 16807) % 2147483647;
    return (seed - 1) / 2147483646;
  };
  for (let i = 0; i < starPositions.length; i += 3) {
    starPositions[i] = (random() - 0.5) * 32;
    starPositions[i + 1] = (random() - 0.5) * 18;
    starPositions[i + 2] = -5 - random() * 8;
  }
  starsGeometry.setAttribute("position", new THREE.BufferAttribute(starPositions, 3));
  const stars = new THREE.Points(
    starsGeometry,
    new THREE.PointsMaterial({ color: "#a8cddd", size: 0.016, transparent: true, opacity: 0.4 }),
  );
  scene.add(stars);

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let elapsed = reducedMotion.matches ? 13 : 0;
  let previousTime = 0;
  let paused = false;
  let visible = true;
  let phase: FlightPhase | undefined;
  let pointerX = 0;
  let pointerY = 0;
  const orbitPosition = new THREE.Vector3();
  const orbitRotation = new THREE.Quaternion();
  const shipUp = new THREE.Vector3(0, 1, 0);
  const tangent = new THREE.Vector3();
  const initialRotation = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, -0.45, 0));

  function render() {
    const pullback = ease((elapsed - 8) / 3.8);
    const launch = ease((elapsed - 6.2) / 3.2);
    const ignition = ease((elapsed - 4.8) / 1.4);
    const currentPhase =
      elapsed < 4.8
        ? "Assembling"
        : elapsed < 6.2
          ? "Ignition"
          : elapsed < 11.8
            ? "Liftoff"
            : "In orbit";
    if (currentPhase !== phase) {
      phase = currentPhase;
      onPhase(phase);
    }
    for (const part of parts)
      part.group.position.copy(part.offset).multiplyScalar(1 - ease((elapsed - part.delay) / 2.5));
    exhaust.visible = elapsed > 4.8;
    exhaust.scale.y = ignition * (0.08 + launch * 0.9) * (1 - pullback * 0.8);
    platform.visible = pullback < 0.99;
    platform.position.set(0, -1 - pullback * 6, 0);
    platform.scale.setScalar(1 - pullback * 0.6);
    const release = ease((elapsed - 5.3) / 0.7);
    for (let i = 0; i < clamps.length; i++) {
      const angle = (i / 3) * Math.PI * 2;
      clamps[i].position.set(Math.sin(angle) * release * 0.5, 0, Math.cos(angle) * release * 0.5);
    }
    wash.material.uniforms.strength.value = ignition * (1 - launch);
    earth.visible = pullback > 0;
    earth.position.set(0, THREE.MathUtils.lerp(-4.5, -0.3, pullback), -0.8);
    earth.scale.setScalar(THREE.MathUtils.lerp(0.65, 1, pullback));
    earth.rotation.set(0.1, 0.7 + elapsed * 0.02, -0.1);
    orbit.position.copy(earth.position);
    orbitMaterial.opacity = pullback * 0.35;
    const angle = 0.95 + Math.max(0, elapsed - 11.8) * 0.18;
    orbitPosition
      .set(Math.cos(angle) * 3.4, Math.sin(angle) * 3.4, 0)
      .applyEuler(orbit.rotation)
      .add(earth.position);
    ship.position.set(0, 0.6 + launch * 2.4, 0).lerp(orbitPosition, pullback);
    ship.scale.setScalar(THREE.MathUtils.lerp(0.9, 0.4, pullback));
    tangent.set(-Math.sin(angle), Math.cos(angle), 0).applyEuler(orbit.rotation).normalize();
    orbitRotation.setFromUnitVectors(shipUp, tangent);
    ship.quaternion.copy(initialRotation).slerp(orbitRotation, pullback);
    if (!reducedMotion.matches) {
      camera.position.x += (pointerX * 0.35 - camera.position.x) * 0.035;
      camera.position.y +=
        (THREE.MathUtils.lerp(3.2, 1.3, pullback) + pointerY * 0.2 - camera.position.y) * 0.035;
    } else {
      camera.position.y = 1.3;
    }
    camera.lookAt(0, 0.25, -0.4);
    renderer.render(scene, camera);
  }
  function animate(now: number) {
    if (previousTime) elapsed += Math.min((now - previousTime) / 1000, 0.1);
    previousTime = now;
    render();
  }
  function syncMotion() {
    previousTime = 0;
    renderer.setAnimationLoop(
      !paused && visible && !document.hidden && !reducedMotion.matches ? animate : null,
    );
    render();
  }
  function resize() {
    const { width, height } = canvas.getBoundingClientRect();
    if (!width || !height) return;
    camera.aspect = width / height;
    camera.fov = 36;
    camera.position.z = Math.max(12.8, 11 / camera.aspect);
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    render();
  }
  function pointer(event: PointerEvent) {
    const rect = canvas.getBoundingClientRect();
    pointerX = (event.clientX - rect.left) / rect.width - 0.5;
    pointerY = (event.clientY - rect.top) / rect.height - 0.5;
  }
  function resetPointer() {
    pointerX = 0;
    pointerY = 0;
  }
  function motionPreference() {
    if (reducedMotion.matches) elapsed = 13;
    syncMotion();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  const intersection = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    syncMotion();
  });
  intersection.observe(canvas);
  canvas.addEventListener("pointermove", pointer);
  canvas.addEventListener("pointerleave", resetPointer);
  document.addEventListener("visibilitychange", syncMotion);
  reducedMotion.addEventListener("change", motionPreference);
  resize();
  syncMotion();

  return {
    setPaused(value: boolean) {
      paused = value;
      syncMotion();
    },
    replay() {
      elapsed = reducedMotion.matches ? 13 : 0;
      paused = false;
      syncMotion();
    },
    dispose() {
      renderer.setAnimationLoop(null);
      observer.disconnect();
      intersection.disconnect();
      canvas.removeEventListener("pointermove", pointer);
      canvas.removeEventListener("pointerleave", resetPointer);
      document.removeEventListener("visibilitychange", syncMotion);
      reducedMotion.removeEventListener("change", motionPreference);
      const geometries = new Set<THREE.BufferGeometry>();
      const materials = new Set<THREE.Material>();
      scene.traverse((node) => {
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
      texture.dispose();
      environmentMap.dispose();
      renderer.dispose();
    },
  };
}

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

const assemblyEnd = 1.15;
const launchStart = 1.4;
const orbitStart = 4.2;
const earthRadius = 12;
const orbitRadius = 17.6;

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
  const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 350);
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
  platform.position.y = -1;
  scene.add(platform);
  const { earth, texture } = createAbstractEarth();
  earth.scale.setScalar(earthRadius / 2.4);
  earth.position.set(0, -earthRadius - 1.07, 0);
  scene.add(earth);

  const orbit = new THREE.Group();
  orbit.rotation.y = -0.55;
  orbit.position.copy(earth.position);
  const orbitPoints = Array.from({ length: 161 }, (_, i) => {
    const angle = (i / 160) * Math.PI * 2;
    return new THREE.Vector3(Math.cos(angle) * orbitRadius, Math.sin(angle) * orbitRadius, 0);
  });
  const orbitMaterial = new THREE.LineBasicMaterial({
    color: "#70b6d1",
    transparent: true,
    opacity: 0,
  });
  orbit.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(orbitPoints), orbitMaterial));
  scene.add(orbit);

  const starsGeometry = new THREE.BufferGeometry();
  const starPositions = new Float32Array(160 * 3);
  let seed = 29;
  const random = () => {
    seed = (seed * 16807) % 2147483647;
    return (seed - 1) / 2147483646;
  };
  for (let i = 0; i < starPositions.length; i += 3) {
    starPositions[i] = (random() - 0.5) * 180;
    starPositions[i + 1] = (random() - 0.5) * 120;
    starPositions[i + 2] = -70 - random() * 50;
  }
  starsGeometry.setAttribute("position", new THREE.BufferAttribute(starPositions, 3));
  const stars = new THREE.Points(
    starsGeometry,
    new THREE.PointsMaterial({ color: "#a8cddd", size: 0.075, transparent: true, opacity: 0.4 }),
  );
  scene.add(stars);

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let elapsed = reducedMotion.matches ? orbitStart : 0;
  let previousTime = 0;
  let paused = false;
  let visible = true;
  let phase: FlightPhase | undefined;
  let pointerX = 0;
  let pointerY = 0;
  let cameraDistance = 12.8;
  const viewOffset = new THREE.Vector2();
  const orbitPosition = new THREE.Vector3();
  const orbitRotation = new THREE.Quaternion();
  const shipUp = new THREE.Vector3(0, 1, 0);
  const tangent = new THREE.Vector3();
  const initialRotation = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, -0.45, 0));

  function render() {
    const pullback = ease((elapsed - 1.7) / (orbitStart - 1.7));
    const launch = Math.pow(THREE.MathUtils.clamp((elapsed - launchStart) / 1.2, 0, 1), 2);
    const bank = ease((elapsed - 2) / (orbitStart - 2));
    const ignition = ease((elapsed - assemblyEnd) / (launchStart - assemblyEnd));
    const currentPhase =
      elapsed < assemblyEnd
        ? "Assembling"
        : elapsed < launchStart
          ? "Ignition"
          : elapsed < orbitStart
            ? "Liftoff"
            : "In orbit";
    if (currentPhase !== phase) {
      phase = currentPhase;
      onPhase(phase);
    }
    for (const part of parts) {
      const remaining = 1 - ease((elapsed - part.delay) / 0.44);
      part.group.position.copy(part.offset).multiplyScalar(remaining);
      part.group.rotation.set(
        part.spin.x * remaining,
        part.spin.y * remaining,
        part.spin.z * remaining,
      );
    }
    exhaust.visible = elapsed > assemblyEnd;
    exhaust.scale.y = ignition * (0.08 + launch * 1.3) * (1 - bank * 0.8);
    platform.scale.setScalar(THREE.MathUtils.lerp(1, 0.06, pullback));
    const release = ease((elapsed - 1.18) / 0.2);
    for (let i = 0; i < clamps.length; i++) {
      const angle = (i / 3) * Math.PI * 2;
      clamps[i].position.set(Math.sin(angle) * release * 0.5, 0, Math.cos(angle) * release * 0.5);
    }
    wash.material.uniforms.strength.value = ignition * (1 - launch);
    earth.rotation.set(0.1, 0.7 + Math.max(0, elapsed - orbitStart) * 0.015, -0.1);
    orbitMaterial.opacity = ease((elapsed - 3.2) / (orbitStart - 3.2)) * 0.3;
    const angle = Math.PI / 2 - bank * 0.72 - Math.max(0, elapsed - orbitStart) * 0.23;
    const radius = THREE.MathUtils.lerp(0.6 - earth.position.y, orbitRadius, launch);
    orbitPosition
      .set(Math.cos(angle) * radius, Math.sin(angle) * radius, 0)
      .applyEuler(orbit.rotation)
      .add(earth.position);
    ship.position.copy(orbitPosition);
    ship.scale.setScalar(0.9);
    tangent.set(Math.sin(angle), -Math.cos(angle), 0).applyEuler(orbit.rotation).normalize();
    orbitRotation.setFromUnitVectors(shipUp, tangent);
    ship.quaternion.copy(initialRotation).slerp(orbitRotation, bank);
    if (!reducedMotion.matches) {
      viewOffset.x += (pointerX * 0.35 - viewOffset.x) * 0.035;
      viewOffset.y += (pointerY * 0.2 - viewOffset.y) * 0.035;
    } else {
      viewOffset.set(0, 0);
    }
    camera.position.set(
      viewOffset.x,
      THREE.MathUtils.lerp(3.2, earth.position.y + 7, pullback) + viewOffset.y,
      cameraDistance * THREE.MathUtils.lerp(1, 4.4, pullback),
    );
    camera.lookAt(0, THREE.MathUtils.lerp(0.25, earth.position.y, pullback), 0);
    renderer.render(scene, camera);
  }
  function animate(now: number) {
    if (previousTime) elapsed += (now - previousTime) / 1000;
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
    cameraDistance = Math.max(12.8, 11 / camera.aspect);
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
    if (reducedMotion.matches) elapsed = orbitStart;
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
      elapsed = reducedMotion.matches ? orbitStart : 0;
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

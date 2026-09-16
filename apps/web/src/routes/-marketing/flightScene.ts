import * as THREE from "three";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { createShip } from "./shipModel";

export type FlightPhase = "Assembling" | "Ignition" | "Liftoff" | "In orbit";

const ease = (value: number) => {
  const t = THREE.MathUtils.clamp(value, 0, 1);
  return t * t * (3 - 2 * t);
};

export async function createFlightScene(
  canvas: HTMLCanvasElement,
  onPhase: (phase: FlightPhase) => void,
) {
  const earthTexture = await new THREE.TextureLoader().loadAsync("/hero/earth.jpg");
  earthTexture.colorSpace = THREE.SRGBColorSpace;
  let renderer: THREE.WebGLRenderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      alpha: true,
      antialias: true,
      powerPreference: "low-power",
    });
  } catch (error) {
    earthTexture.dispose();
    throw error;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.25;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 100);
  camera.position.set(0, 0.2, 12.8);
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

  const { ship, parts, flames, label } = createShip();
  scene.add(ship);
  const earth = new THREE.Group();
  const planet = new THREE.Mesh(
    new THREE.SphereGeometry(2.4, 72, 48),
    new THREE.MeshStandardMaterial({
      map: earthTexture,
      roughness: 0.92,
      metalness: 0.08,
      color: "#94b5cb",
    }),
  );
  earth.add(planet);
  const atmosphere = new THREE.Mesh(
    new THREE.SphereGeometry(2.47, 64, 40),
    new THREE.ShaderMaterial({
      uniforms: { tint: { value: new THREE.Color("#3293db") } },
      vertexShader: `varying vec3 vNormal; varying vec3 vView;
      void main() { vec4 p = modelViewMatrix * vec4(position, 1.0); vNormal = normalize(normalMatrix * normal); vView = normalize(-p.xyz); gl_Position = projectionMatrix * p; }`,
      fragmentShader: `uniform vec3 tint; varying vec3 vNormal; varying vec3 vView;
      void main() { float rim = pow(1.0 - max(dot(normalize(vNormal), normalize(vView)), 0.0), 4.0); gl_FragColor = vec4(tint, rim * 0.65); }`,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    }),
  );
  earth.add(atmosphere);
  scene.add(earth);

  const orbit = new THREE.Group();
  orbit.rotation.x = 0.7;
  const orbitPoints = Array.from({ length: 161 }, (_, i) => {
    const angle = (i / 160) * Math.PI * 2;
    return new THREE.Vector3(Math.cos(angle) * 3.65, Math.sin(angle) * 3.65, 0);
  });
  const orbitMaterial = new THREE.LineBasicMaterial({
    color: "#70b6d1",
    transparent: true,
    opacity: 0,
  });
  orbit.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(orbitPoints), orbitMaterial));
  scene.add(orbit);

  const starsGeometry = new THREE.BufferGeometry();
  const starPositions = new Float32Array(240 * 3);
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
    new THREE.PointsMaterial({ color: "#a8cddd", size: 0.018, transparent: true, opacity: 0.65 }),
  );
  scene.add(stars);

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let elapsed = reducedMotion.matches ? 12 : 0;
  let previousTime = 0;
  let paused = false;
  let visible = true;
  let phase: FlightPhase | undefined;
  let pointerX = 0;
  let pointerY = 0;
  let narrow = false;
  const orbitPosition = new THREE.Vector3();
  const orbitRotation = new THREE.Quaternion();
  const shipUp = new THREE.Vector3(0, 1, 0);
  const tangent = new THREE.Vector3();
  const initialRotation = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.22, -0.48, -0.48));

  function render() {
    const pullback = ease((elapsed - 6.8) / 3.5);
    const launch = ease((elapsed - 5) / 2.8);
    const currentPhase =
      elapsed < 3.8
        ? "Assembling"
        : elapsed < 5.2
          ? "Ignition"
          : elapsed < 10.3
            ? "Liftoff"
            : "In orbit";
    if (currentPhase !== phase) {
      phase = currentPhase;
      onPhase(phase);
    }
    for (const part of parts)
      part.group.position.copy(part.offset).multiplyScalar(1 - ease((elapsed - part.delay) / 2.8));
    for (const flame of flames) {
      flame.visible = elapsed > 3.8;
      flame.scale.y = (0.3 + ease((elapsed - 3.8) / 1.6) * 1.3) * (1 - pullback * 0.65);
    }
    earth.position.set(0, THREE.MathUtils.lerp(-6.5, -0.35, pullback), -2);
    earth.scale.setScalar(THREE.MathUtils.lerp(2, 1, pullback));
    planet.rotation.set(0.16, 0.5 + elapsed * 0.025, 0.08);
    orbit.position.copy(earth.position);
    orbitMaterial.opacity = pullback * 0.22;
    const angle = 0.95 + Math.max(0, elapsed - 10.3) * 0.18;
    orbitPosition
      .set(Math.cos(angle) * 3.65, Math.sin(angle) * 3.65, 0)
      .applyEuler(orbit.rotation)
      .add(earth.position);
    ship.position.set(0, 0.3 + launch * 1.6, 0).lerp(orbitPosition, pullback);
    ship.scale.setScalar(THREE.MathUtils.lerp(narrow ? 0.84 : 1.05, 0.36, pullback));
    tangent.set(-Math.sin(angle), Math.cos(angle), 0).applyEuler(orbit.rotation).normalize();
    orbitRotation.setFromUnitVectors(shipUp, tangent);
    ship.quaternion.copy(initialRotation).slerp(orbitRotation, pullback);
    if (!reducedMotion.matches) {
      camera.position.x += (pointerX * 0.35 - camera.position.x) * 0.035;
      camera.position.y += (0.2 + pointerY * 0.2 - camera.position.y) * 0.035;
    }
    camera.lookAt(0, 0, -0.4);
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
    narrow = width < 600;
    camera.aspect = width / height;
    camera.fov = narrow ? 48 : 36;
    camera.position.z = narrow ? 15 : 12.8;
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
    if (reducedMotion.matches) elapsed = 12;
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
      elapsed = reducedMotion.matches ? 12 : 0;
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
      earthTexture.dispose();
      label.dispose();
      environmentMap.dispose();
      renderer.dispose();
    },
  };
}

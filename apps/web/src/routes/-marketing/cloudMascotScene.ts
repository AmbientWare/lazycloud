import * as THREE from "three";
import { createAsciiRenderer } from "@/components/canvasui/asciiRenderer";

export function createCloudMascotScene(canvas: HTMLCanvasElement, stage: HTMLDivElement) {
  const renderer = createAsciiRenderer(canvas);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-3, 3, 2.5, -2.5, 0.1, 30);
  camera.position.z = 8;
  const cloud = new THREE.Group();
  const face = new THREE.Group();
  cloud.add(face);
  scene.add(cloud, new THREE.HemisphereLight("#c8eeff", "#12324d", 1.5));
  const light = new THREE.DirectionalLight("#e4faff", 3.5);
  light.position.set(-4, 6, 3);
  scene.add(light);
  const material = new THREE.MeshStandardMaterial({
    color: "#87dcfa",
    roughness: 0.62,
    metalness: 0,
  });
  const ink = new THREE.MeshBasicMaterial({ color: "#092c43" });
  const highlight = new THREE.MeshBasicMaterial({ color: "#e8faff" });
  const geometries: THREE.BufferGeometry[] = [];

  const outline = new THREE.Path();
  outline.moveTo(36, 52);
  outline.bezierCurveTo(41, 22, 83, 20, 94, 54);
  outline.bezierCurveTo(122, 56, 125, 96, 98, 101);
  outline.lineTo(35, 101);
  outline.bezierCurveTo(6, 101, 3, 60, 36, 52);
  const contour = outline
    .getPoints(100)
    .map((p) => new THREE.Vector2((p.x - 64) / 29, (65 - p.y) / 29));
  const bodyGeometry = new THREE.ExtrudeGeometry(new THREE.Shape(contour), {
    depth: 0.38,
    bevelEnabled: true,
    bevelThickness: 0.42,
    bevelSize: 0.24,
    bevelSegments: 12,
    steps: 1,
  });
  bodyGeometry.translate(0, 0, -0.19);
  geometries.push(bodyGeometry);
  cloud.add(new THREE.Mesh(bodyGeometry, material));

  const eyes: THREE.Group[] = [];
  const eyeGeometry = new THREE.SphereGeometry(0.16, 24, 20);
  const glintGeometry = new THREE.SphereGeometry(0.046, 16, 12);
  geometries.push(eyeGeometry, glintGeometry);
  for (const x of [-0.5, 0.5]) {
    const eye = new THREE.Group();
    const mesh = new THREE.Mesh(eyeGeometry, ink);
    mesh.scale.set(1, 1.35, 0.5);
    eye.position.set(x, -0.17, 0);
    eye.add(mesh);
    const glint = new THREE.Mesh(glintGeometry, highlight);
    glint.position.set(-0.04, 0.07, 0.08);
    eye.add(glint);
    eyes.push(eye);
    face.add(eye);
  }
  const smile = new THREE.QuadraticBezierCurve3(
    new THREE.Vector3(-0.19, -0.51, 0),
    new THREE.Vector3(0.01, -0.72, 0),
    new THREE.Vector3(0.23, -0.49, 0),
  );
  const smileGeometry = new THREE.TubeGeometry(smile, 32, 0.045, 10, false);
  geometries.push(smileGeometry);
  face.add(new THREE.Mesh(smileGeometry, ink));
  face.position.z = 0.64;

  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const pointer = new THREE.Vector2();
  const look = new THREE.Vector2();
  const hero = stage.closest("section");
  let visible = true;
  let disposed = false;
  let animation = 0;
  let previousTime = 0;
  let elapsed = 0;

  function draw(delta = 0) {
    if (motion.matches) look.set(0, 0);
    else look.lerp(pointer, 1 - Math.exp(-delta * 7));
    cloud.rotation.set(-0.07 - look.y * 0.18, -0.15 + look.x * 0.35, -0.035 + look.x * 0.07);
    cloud.position.y = 0.18 + (motion.matches ? 0 : Math.sin(elapsed * 1.4) * 0.08);
    face.position.x = look.x * 0.085;
    face.position.y = look.y * 0.055;
    const cycle = elapsed % 7.6;
    const blinkAt = (start: number) => {
      const phase = (cycle - start) / 0.24;
      return phase > 0 && phase < 1 ? Math.sin(phase * Math.PI) ** 2 : 0;
    };
    const blink = motion.matches ? 0 : Math.max(blinkAt(2.8), blinkAt(6.5), blinkAt(6.86));
    for (const eye of eyes) eye.scale.y = 1 - blink * 0.93;
    renderer.render(scene, camera);
  }
  function tick(now: number) {
    if (disposed) return;
    const delta = previousTime ? Math.min((now - previousTime) / 1000, 0.05) : 0;
    previousTime = now;
    elapsed += delta;
    draw(delta);
    animation = requestAnimationFrame(tick);
  }
  function syncMotion() {
    cancelAnimationFrame(animation);
    previousTime = 0;
    if (!disposed && visible && !document.hidden && !motion.matches)
      animation = requestAnimationFrame(tick);
    draw();
  }
  function resize() {
    const { width, height } = canvas.getBoundingClientRect();
    if (!width || !height) return;
    const aspect = width / height;
    const halfHeight = Math.max(2.05, 2.65 / aspect);
    camera.left = -halfHeight * aspect;
    camera.right = halfHeight * aspect;
    camera.top = halfHeight;
    camera.bottom = -halfHeight;
    camera.updateProjectionMatrix();
    renderer.resize(width, height);
    draw();
  }
  function follow(event: PointerEvent) {
    if (event.pointerType === "touch" || motion.matches) return;
    const rect = stage.getBoundingClientRect();
    pointer.set(
      THREE.MathUtils.clamp((event.clientX - rect.left - rect.width / 2) / (rect.width / 2), -1, 1),
      THREE.MathUtils.clamp(
        -(event.clientY - rect.top - rect.height / 2) / (rect.height / 2),
        -1,
        1,
      ),
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
  intersection.observe(stage);
  hero?.addEventListener("pointermove", follow);
  hero?.addEventListener("pointerleave", reset);
  window.addEventListener("blur", reset);
  document.addEventListener("visibilitychange", syncMotion);
  motion.addEventListener("change", syncMotion);
  resize();
  syncMotion();
  return {
    dispose() {
      disposed = true;
      cancelAnimationFrame(animation);
      resizeObserver.disconnect();
      intersection.disconnect();
      hero?.removeEventListener("pointermove", follow);
      hero?.removeEventListener("pointerleave", reset);
      window.removeEventListener("blur", reset);
      document.removeEventListener("visibilitychange", syncMotion);
      motion.removeEventListener("change", syncMotion);
      for (const geometry of geometries) geometry.dispose();
      material.dispose();
      ink.dispose();
      highlight.dispose();
      renderer.dispose();
    },
  };
}

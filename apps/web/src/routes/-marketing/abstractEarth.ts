import * as THREE from "three";
import land from "./earthLand.json";

export function createAbstractEarth() {
  const earth = new THREE.Group();
  const map = document.createElement("canvas");
  map.width = 2048;
  map.height = 1024;
  const context = map.getContext("2d");
  if (!context) throw new Error("Canvas 2D is required to draw the globe.");
  context.fillStyle = "#091520";
  context.fillRect(0, 0, map.width, map.height);
  context.fillStyle = "#213d4f";
  context.strokeStyle = "#4e8298";
  context.lineWidth = 1.5;
  for (const ring of land) {
    context.beginPath();
    ring.forEach(([longitude, latitude], index) => {
      const x = ((longitude + 180) / 360) * map.width;
      const y = ((90 - latitude) / 180) * map.height;
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.closePath();
    context.fill();
    context.stroke();
  }
  const texture = new THREE.CanvasTexture(map);
  texture.colorSpace = THREE.SRGBColorSpace;
  const globe = new THREE.Mesh(
    new THREE.SphereGeometry(2.4, 64, 40),
    new THREE.MeshBasicMaterial({ map: texture }),
  );
  earth.add(globe);
  const grid = new THREE.Group();
  const lines = new THREE.LineBasicMaterial({ color: "#5c95b0", transparent: true, opacity: 0.12 });
  for (let latitude = -60; latitude <= 60; latitude += 30) {
    const phi = THREE.MathUtils.degToRad(latitude);
    const points = Array.from({ length: 97 }, (_, i) => {
      const angle = (i / 96) * Math.PI * 2;
      return new THREE.Vector3(
        Math.cos(angle) * Math.cos(phi) * 2.408,
        Math.sin(phi) * 2.408,
        Math.sin(angle) * Math.cos(phi) * 2.408,
      );
    });
    grid.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), lines));
  }
  for (let longitude = 0; longitude < 180; longitude += 30) {
    const theta = THREE.MathUtils.degToRad(longitude);
    const points = Array.from({ length: 97 }, (_, i) => {
      const angle = (i / 96) * Math.PI * 2;
      return new THREE.Vector3(
        Math.cos(angle) * Math.cos(theta) * 2.408,
        Math.sin(angle) * 2.408,
        Math.cos(angle) * Math.sin(theta) * 2.408,
      );
    });
    grid.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), lines));
  }
  earth.add(grid);
  const atmosphere = new THREE.Mesh(
    new THREE.SphereGeometry(2.44, 64, 40),
    new THREE.ShaderMaterial({
      vertexShader: `varying vec3 vNormal; varying vec3 vView; void main() { vec4 p = modelViewMatrix * vec4(position, 1.0); vNormal = normalize(normalMatrix * normal); vView = normalize(-p.xyz); gl_Position = projectionMatrix * p; }`,
      fragmentShader: `varying vec3 vNormal; varying vec3 vView; void main() { float rim = pow(1.0 - max(dot(normalize(vNormal), normalize(vView)), 0.0), 5.0); gl_FragColor = vec4(0.25, 0.65, 0.85, rim * 0.3); }`,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    }),
  );
  earth.add(atmosphere);
  return { earth, texture };
}

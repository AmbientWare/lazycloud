import * as THREE from "three";
import { FullScreenQuad } from "three/addons/postprocessing/Pass.js";

export function createDevboxFocus(renderer: THREE.WebGLRenderer, camera: THREE.PerspectiveCamera) {
  const target = new THREE.WebGLRenderTarget(1, 1, {
    type: THREE.HalfFloatType,
    samples: 4,
    depthTexture: new THREE.DepthTexture(1, 1, THREE.UnsignedIntType),
  });
  const uniforms = {
    colorMap: { value: target.texture },
    depthMap: { value: target.depthTexture },
    viewport: { value: new THREE.Vector2(1, 1) },
    focusScale: { value: new THREE.Vector2(1, 1) },
    focalPoint: { value: new THREE.Vector2() },
    focalDepth: { value: 0 },
    cameraNear: { value: camera.near },
    cameraFar: { value: camera.far },
    expansion: { value: 0 },
  };
  const material = new THREE.ShaderMaterial({
    uniforms,
    depthTest: false,
    depthWrite: false,
    vertexShader: `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = vec4(position.xy, 0.0, 1.0);
      }
    `,
    fragmentShader: `
      #include <packing>
      uniform sampler2D colorMap;
      uniform sampler2D depthMap;
      uniform vec2 viewport;
      uniform vec2 focusScale;
      uniform vec2 focalPoint;
      uniform float focalDepth;
      uniform float cameraNear;
      uniform float cameraFar;
      uniform float expansion;
      varying vec2 vUv;

      float softness(vec2 uv) {
        float depth = -perspectiveDepthToViewZ(
          texture2D(depthMap, uv).r, cameraNear, cameraFar
        );
        float distanceFromFocus = length(
          (uv - focalPoint) * focusScale
        );
        float peripheral = smoothstep(0.2, 0.95, distanceFromFocus);
        float distant = smoothstep(0.75, 7.0, depth - focalDepth);
        return max(peripheral, distant) * expansion;
      }

      void main() {
        float blur = softness(vUv);
        vec4 color = vec4(0.0);
        float totalWeight = 0.0;
        for (int i = 0; i < 25; i++) {
          float radius = sqrt(float(i) / 24.0);
          float angle = float(i) * 2.399963;
          vec2 offset = vec2(cos(angle), sin(angle)) * radius * blur * 3.0 / viewport;
          vec2 sampleUv = vUv + offset;
          float weight = exp(-2.0 * radius * radius);
          vec4 sampleColor = texture2D(colorMap, sampleUv);
          float visibility = mix(0.9, 0.025, softness(sampleUv));
          color += sampleColor * visibility * weight;
          totalWeight += weight;
        }
        color /= totalWeight;
        // The render target contains premultiplied alpha. Convert straight RGB
        // for tone mapping, then restore premultiplication for the page canvas.
        gl_FragColor = vec4(color.rgb / max(color.a, 0.0001), color.a);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
        gl_FragColor.rgb *= gl_FragColor.a;
      }
    `,
  });
  const quad = new FullScreenQuad(material);
  const projectedFocus = new THREE.Vector3();

  return {
    resize(width: number, height: number) {
      const pixelRatio = renderer.getPixelRatio();
      target.setSize(Math.round(width * pixelRatio), Math.round(height * pixelRatio));
      uniforms.viewport.value.set(width, height);
      uniforms.focusScale.value.set(width < 1024 ? 1 : width / height, width < 1024 ? 2 : 1);
    },
    render(scene: THREE.Scene, focus: THREE.Vector3, expansion: number) {
      camera.updateMatrixWorld();
      projectedFocus.copy(focus).project(camera);
      uniforms.focalPoint.value.set(projectedFocus.x * 0.5 + 0.5, projectedFocus.y * 0.5 + 0.5);
      uniforms.focalDepth.value = camera.position.length() - 1;
      uniforms.expansion.value = expansion;
      renderer.setRenderTarget(target);
      renderer.render(scene, camera);
      renderer.setRenderTarget(null);
      quad.render(renderer);
    },
    dispose() {
      target.dispose();
      material.dispose();
      quad.dispose();
    },
  };
}

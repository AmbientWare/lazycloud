"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type Ref,
} from "react";

import { createRectCache } from "./rect-cache";

export interface CloudsOptions {
  paused?: boolean;
  scale?: number;
  speed?: number;
  scrollWithContent?: boolean;
  cover?: number;
  density?: number;
  blur?: number;
  shading?: number;
  color: [number, number, number];
  opacity?: number;
  wind?: number;
  windRadius?: number;
  quality?: number;
}

export interface CloudsElements {
  content: HTMLElement;
  output: HTMLCanvasElement;
}

export interface CloudsInstance {
  setOptions: (options: CloudsOptions) => void;
  destroy: () => void;
}

const DEFAULTS: Required<Omit<CloudsOptions, "color">> = {
  paused: false,
  scale: 1,
  speed: 0.6,
  scrollWithContent: true,
  cover: 0.1,
  density: 2.5,
  blur: 0,
  shading: 0.1,
  opacity: 0.64,
  wind: 0.6,
  windRadius: 350,
  quality: 1,
};

const VERT = `#version 300 es
precision highp float;
layout(location = 0) in vec2 aPos;
void main () {
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

const FIELD_FRAG = `#version 300 es
precision highp float;
out vec4 outColor;
uniform vec2 uResolution;
uniform vec2 uOffset;
uniform float uTime;
uniform float uScale;
uniform float uCover;
uniform float uDensity;

const mat2 m = mat2(1.6, 1.2, -1.2, 1.6);

vec2 hash (vec2 p) {
  p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
  return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
}

float noise (vec2 p) {
  const float K1 = 0.366025404;
  const float K2 = 0.211324865;
  vec2 i = floor(p + (p.x + p.y) * K1);
  vec2 a = p - i + (i.x + i.y) * K2;
  vec2 o = (a.x > a.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
  vec2 b = a - o + K2;
  vec2 c = a - 1.0 + 2.0 * K2;
  vec3 h = max(0.5 - vec3(dot(a, a), dot(b, b), dot(c, c)), 0.0);
  vec3 n = h * h * h * h
    * vec3(dot(a, hash(i)), dot(b, hash(i + o)), dot(c, hash(i + 1.0)));
  return dot(n, vec3(70.0));
}

float fbm (vec2 n) {
  float total = 0.0;
  float amplitude = 0.1;
  for (int i = 0; i < 7; i++) {
    total += noise(n) * amplitude;
    n = m * n;
    amplitude *= 0.4;
  }
  return total;
}

void main () {
  vec2 p = gl_FragCoord.xy / uResolution + uOffset;
  vec2 asp = vec2(uResolution.x / uResolution.y, 1.0);
  float q = fbm(p * asp * uScale * 0.5);

  float r = 0.0;
  vec2 uv = p * asp * uScale;
  uv -= q - uTime;
  float weight = 0.8;
  for (int i = 0; i < 8; i++) {
    r += abs(weight * noise(uv));
    uv = m * uv + uTime;
    weight *= 0.7;
  }

  float f = 0.0;
  uv = p * asp * uScale;
  uv -= q - uTime;
  weight = 0.7;
  for (int i = 0; i < 8; i++) {
    f += weight * noise(uv);
    uv = m * uv + uTime;
    weight *= 0.6;
  }
  f *= r + f;

  float c = 0.0;
  float t2 = uTime * 2.0;
  uv = p * asp * uScale * 2.0;
  uv -= q - t2;
  weight = 0.4;
  for (int i = 0; i < 7; i++) {
    c += weight * noise(uv);
    uv = m * uv + t2;
    weight *= 0.6;
  }

  float c1 = 0.0;
  float t3 = uTime * 3.0;
  uv = p * asp * uScale * 3.0;
  uv -= q - t3;
  weight = 0.4;
  for (int i = 0; i < 7; i++) {
    c1 += abs(weight * noise(uv));
    uv = m * uv + t3;
    weight *= 0.6;
  }
  c += c1;

  float coverage = clamp(uCover + uDensity * f * r + c, 0.0, 1.0);
  outColor = vec4(coverage, clamp(c, 0.0, 1.0), 0.0, 1.0);
}`;

const WIND_FRAG = `#version 300 es
precision highp float;
out vec4 outColor;
uniform sampler2D uPrev;
uniform vec2 uResolution;
uniform float uDecay;
uniform vec2 uA;
uniform vec2 uB;
uniform float uRadius;
uniform float uStrength;

void main () {
  vec2 uv = gl_FragCoord.xy / uResolution;
  float prev = texture(uPrev, uv).r * uDecay;
  vec2 asp = vec2(uResolution.x / uResolution.y, 1.0);
  vec2 p = uv * asp;
  vec2 a = uA * asp;
  vec2 b = uB * asp;
  vec2 pa = p - a;
  vec2 ba = b - a;
  float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-6), 0.0, 1.0);
  float d = length(pa - ba * h) / max(uRadius, 1e-4);
  float stamp = exp(-d * d * 3.0) * uStrength;
  outColor = vec4(clamp(prev + stamp, 0.0, 1.0), 0.0, 0.0, 1.0);
}`;

const COMPOSITE_FRAG = `#version 300 es
precision highp float;
out vec4 outColor;
uniform sampler2D uField;
uniform sampler2D uWind;
uniform vec2 uResolution;
uniform vec3 uBase;
uniform float uBlur;
uniform float uShading;
uniform float uOpacity;
uniform float uWindAmt;

void main () {
  vec2 uv = gl_FragCoord.xy / uResolution;
  float blurLod = uBlur * 5.0;
  vec2 field = mix(texture(uField, uv).rg, textureLod(uField, uv, blurLod).rg, uBlur);
  float wind = texture(uWind, uv).r * uWindAmt;
  float cov = field.r - wind;
  float mist = smoothstep(0.04, 0.9, cov);
  float cloudA = mist * uOpacity;

  float lum = dot(uBase, vec3(0.299, 0.587, 0.114));
  float sh = clamp(field.g, 0.0, 1.0);
  float k = uShading * 0.35;
  vec3 cloudRGB = lum > 0.5
    ? uBase - vec3((1.0 - sh) * k)
    : uBase + vec3(sh * k);
  cloudRGB = clamp(cloudRGB, 0.0, 1.0);

  outColor = vec4(cloudRGB * cloudA, cloudA);
}`;

function createClouds(elements: CloudsElements, options: CloudsOptions): CloudsInstance | null {
  const config = { ...DEFAULTS, ...options };
  const { content, output } = elements;

  const gl = output.getContext("webgl2", {
    alpha: true,
    depth: false,
    stencil: false,
    antialias: false,
    premultipliedAlpha: true,
  });
  if (!gl || gl.isContextLost()) return null;

  function compile(type: number, text: string): WebGLShader {
    const shader = gl!.createShader(type)!;
    gl!.shaderSource(shader, text);
    gl!.compileShader(shader);
    if (!gl!.getShaderParameter(shader, gl!.COMPILE_STATUS)) {
      console.error("Clouds shader error:", gl!.getShaderInfoLog(shader));
    }
    return shader;
  }

  function link(fragSource: string) {
    const vs = compile(gl!.VERTEX_SHADER, VERT);
    const fs = compile(gl!.FRAGMENT_SHADER, fragSource);
    const program = gl!.createProgram()!;
    gl!.attachShader(program, vs);
    gl!.attachShader(program, fs);
    gl!.linkProgram(program);
    const uniforms: Record<string, WebGLUniformLocation> = {};
    const count = gl!.getProgramParameter(program, gl!.ACTIVE_UNIFORMS);
    for (let i = 0; i < count; i++) {
      const info = gl!.getActiveUniform(program, i)!;
      uniforms[info.name] = gl!.getUniformLocation(program, info.name)!;
    }
    return { program, vs, fs, uniforms };
  }

  const field = link(FIELD_FRAG);
  const windPass = link(WIND_FRAG);
  const composite = link(COMPOSITE_FRAG);

  const quad = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, quad);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

  const fieldTexture = gl.createTexture()!;
  gl.bindTexture(gl.TEXTURE_2D, fieldTexture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

  function makeWindTexture() {
    const texture = gl!.createTexture()!;
    gl!.bindTexture(gl!.TEXTURE_2D, texture);
    gl!.texParameteri(gl!.TEXTURE_2D, gl!.TEXTURE_MIN_FILTER, gl!.LINEAR);
    gl!.texParameteri(gl!.TEXTURE_2D, gl!.TEXTURE_MAG_FILTER, gl!.LINEAR);
    gl!.texParameteri(gl!.TEXTURE_2D, gl!.TEXTURE_WRAP_S, gl!.CLAMP_TO_EDGE);
    gl!.texParameteri(gl!.TEXTURE_2D, gl!.TEXTURE_WRAP_T, gl!.CLAMP_TO_EDGE);
    return texture;
  }
  const windTextures = [makeWindTexture(), makeWindTexture()];
  let windIndex = 0;

  const fbo = gl.createFramebuffer();

  let fieldW = 0;
  let fieldH = 0;

  function syncCanvasSize() {
    const cw = content.clientWidth;
    const ch = content.clientHeight;
    if (cw > 0 && ch > 0) {
      const wpx = `${cw}px`;
      const hpx = `${ch}px`;
      if (output.style.width !== wpx) output.style.width = wpx;
      if (output.style.height !== hpx) output.style.height = hpx;
    }
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(output.clientWidth * dpr));
    const height = Math.max(1, Math.round(output.clientHeight * dpr));
    if (output.width !== width || output.height !== height) {
      output.width = width;
      output.height = height;
    }
    const quality = Math.min(Math.max(config.quality, 0.2), 1);
    const cap = 1440 / Math.max(output.clientWidth, 1);
    const q = Math.min(quality, cap);
    const nextW = Math.max(16, Math.round(output.clientWidth * q));
    const nextH = Math.max(16, Math.round(output.clientHeight * q));
    if (nextW !== fieldW || nextH !== fieldH) {
      fieldW = nextW;
      fieldH = nextH;
      gl!.bindTexture(gl!.TEXTURE_2D, fieldTexture);
      gl!.texImage2D(
        gl!.TEXTURE_2D,
        0,
        gl!.RGBA,
        fieldW,
        fieldH,
        0,
        gl!.RGBA,
        gl!.UNSIGNED_BYTE,
        null,
      );
      gl!.generateMipmap(gl!.TEXTURE_2D);
      for (const texture of windTextures) {
        gl!.bindTexture(gl!.TEXTURE_2D, texture);
        gl!.texImage2D(
          gl!.TEXTURE_2D,
          0,
          gl!.RGBA,
          fieldW,
          fieldH,
          0,
          gl!.RGBA,
          gl!.UNSIGNED_BYTE,
          null,
        );
      }
    }
  }

  syncCanvasSize();

  let pointerX = 0.5;
  let pointerY = 0.5;
  let prevPointerX = 0.5;
  let prevPointerY = 0.5;
  let hasPointer = false;
  let lastPointerMove = Number.NEGATIVE_INFINITY;

  let time = Math.random() * 64;

  function render(delta: number) {
    gl!.bindFramebuffer(gl!.FRAMEBUFFER, fbo);
    gl!.framebufferTexture2D(
      gl!.FRAMEBUFFER,
      gl!.COLOR_ATTACHMENT0,
      gl!.TEXTURE_2D,
      fieldTexture,
      0,
    );
    gl!.viewport(0, 0, fieldW, fieldH);
    gl!.useProgram(field.program);
    gl!.uniform2f(field.uniforms.uResolution, fieldW, fieldH);
    gl!.uniform2f(
      field.uniforms.uOffset,
      config.scrollWithContent ? content.scrollLeft / Math.max(content.clientWidth, 1) : 0,
      config.scrollWithContent ? -content.scrollTop / Math.max(content.clientHeight, 1) : 0,
    );
    gl!.uniform1f(field.uniforms.uTime, time);
    gl!.uniform1f(field.uniforms.uScale, Math.max(config.scale, 0.05));
    gl!.uniform1f(field.uniforms.uCover, Math.max(config.cover, 0));
    gl!.uniform1f(field.uniforms.uDensity, Math.max(config.density, 0));
    gl!.drawArrays(gl!.TRIANGLE_STRIP, 0, 4);

    const prevWind = windTextures[windIndex];
    const nextWind = windTextures[1 - windIndex];
    windIndex = 1 - windIndex;
    gl!.framebufferTexture2D(gl!.FRAMEBUFFER, gl!.COLOR_ATTACHMENT0, gl!.TEXTURE_2D, nextWind, 0);
    gl!.useProgram(windPass.program);
    gl!.activeTexture(gl!.TEXTURE0);
    gl!.bindTexture(gl!.TEXTURE_2D, prevWind);
    gl!.uniform1i(windPass.uniforms.uPrev, 0);
    gl!.uniform2f(windPass.uniforms.uResolution, fieldW, fieldH);
    gl!.uniform1f(windPass.uniforms.uDecay, Math.pow(0.5, delta / 0.7));
    const moved = Math.hypot(pointerX - prevPointerX, pointerY - prevPointerY);
    const stamping = !config.paused && hasPointer && moved > 0;
    gl!.uniform2f(windPass.uniforms.uA, prevPointerX, prevPointerY);
    gl!.uniform2f(windPass.uniforms.uB, pointerX, pointerY);
    gl!.uniform1f(
      windPass.uniforms.uRadius,
      Math.max(config.windRadius, 1) / Math.max(output.clientHeight, 1),
    );
    gl!.uniform1f(windPass.uniforms.uStrength, stamping ? Math.min(0.2 + moved * 12, 1) * 0.5 : 0);
    gl!.drawArrays(gl!.TRIANGLE_STRIP, 0, 4);
    prevPointerX = pointerX;
    prevPointerY = pointerY;

    gl!.bindFramebuffer(gl!.FRAMEBUFFER, null);
    gl!.bindTexture(gl!.TEXTURE_2D, fieldTexture);
    gl!.generateMipmap(gl!.TEXTURE_2D);

    gl!.viewport(0, 0, output.width, output.height);
    gl!.useProgram(composite.program);
    gl!.activeTexture(gl!.TEXTURE0);
    gl!.bindTexture(gl!.TEXTURE_2D, fieldTexture);
    gl!.uniform1i(composite.uniforms.uField, 0);
    gl!.activeTexture(gl!.TEXTURE1);
    gl!.bindTexture(gl!.TEXTURE_2D, nextWind);
    gl!.uniform1i(composite.uniforms.uWind, 1);
    gl!.uniform2f(composite.uniforms.uResolution, output.width, output.height);
    gl!.uniform3f(composite.uniforms.uBase, ...config.color);
    gl!.uniform1f(composite.uniforms.uBlur, Math.min(Math.max(config.blur, 0), 1));
    gl!.uniform1f(composite.uniforms.uOpacity, Math.min(Math.max(config.opacity, 0), 1));
    gl!.uniform1f(composite.uniforms.uShading, Math.max(config.shading, 0));
    gl!.uniform1f(composite.uniforms.uWindAmt, Math.min(Math.max(config.wind, 0), 1));
    gl!.drawArrays(gl!.TRIANGLE_STRIP, 0, 4);
  }

  let raf = 0;
  let lastTime = performance.now();
  let destroyed = false;
  let running = false;
  let visible = true;

  const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  let reducedMotion = motionQuery.matches;

  function frame(now: number) {
    if (destroyed) return;
    if (!visible) {
      running = false;
      return;
    }
    const delta = Math.min((now - lastTime) / 1000, 1 / 30);
    lastTime = now;
    const driftActive = !config.paused && !reducedMotion && config.speed !== 0;
    if (driftActive) time += delta * config.speed * 0.03;
    render(config.paused ? 0 : delta);
    const windActive = !config.paused && now - lastPointerMove < 3000;
    if (!driftActive && !windActive) {
      running = false;
      return;
    }
    raf = requestAnimationFrame(frame);
  }

  function start() {
    if (destroyed || running || !visible) return;
    running = true;
    lastTime = performance.now();
    raf = requestAnimationFrame(frame);
  }

  start();

  function onMotionChange() {
    reducedMotion = motionQuery.matches;
    start();
  }
  motionQuery.addEventListener("change", onMotionChange);

  const observer = new ResizeObserver(() => {
    syncCanvasSize();
    start();
  });
  observer.observe(output);
  observer.observe(content);

  const intersection = new IntersectionObserver((entries) => {
    visible = entries[entries.length - 1]?.isIntersecting ?? true;
    if (visible) start();
  });
  intersection.observe(output);

  const rectCache = createRectCache(output);

  function onPointerMove(event: PointerEvent) {
    if (config.paused) return;
    const rect = rectCache.current;
    const x = (event.clientX - rect.left) / Math.max(rect.width, 1);
    const y = 1 - (event.clientY - rect.top) / Math.max(rect.height, 1);
    if (!hasPointer) {
      prevPointerX = x;
      prevPointerY = y;
      hasPointer = true;
    }
    pointerX = x;
    pointerY = y;
    lastPointerMove = performance.now();
    start();
  }

  function onPointerLeave() {
    hasPointer = false;
  }

  function onScroll() {
    if (!config.paused) start();
  }

  content.addEventListener("pointermove", onPointerMove, { passive: true });
  content.addEventListener("pointerleave", onPointerLeave, { passive: true });
  content.addEventListener("scroll", onScroll, { passive: true });

  return {
    setOptions(next) {
      if (
        !Object.entries(next).some(([key, value]) => config[key as keyof CloudsOptions] !== value)
      )
        return;
      Object.assign(config, next);
      syncCanvasSize();
      start();
    },
    destroy() {
      destroyed = true;
      rectCache.destroy();
      cancelAnimationFrame(raf);
      observer.disconnect();
      intersection.disconnect();
      motionQuery.removeEventListener("change", onMotionChange);
      content.removeEventListener("pointermove", onPointerMove);
      content.removeEventListener("pointerleave", onPointerLeave);
      content.removeEventListener("scroll", onScroll);
      gl!.deleteTexture(fieldTexture);
      gl!.deleteTexture(windTextures[0]);
      gl!.deleteTexture(windTextures[1]);
      gl!.deleteFramebuffer(fbo);
      gl!.deleteProgram(field.program);
      gl!.deleteProgram(windPass.program);
      gl!.deleteProgram(composite.program);
      gl!.deleteShader(field.vs);
      gl!.deleteShader(field.fs);
      gl!.deleteShader(windPass.vs);
      gl!.deleteShader(windPass.fs);
      gl!.deleteShader(composite.vs);
      gl!.deleteShader(composite.fs);
      gl!.deleteBuffer(quad);
    },
  };
}

export interface CloudsProps extends CloudsOptions {
  children: ReactNode;
  className?: string;
  contentClassName?: string;
  contentRef?: Ref<HTMLDivElement>;
}

function setRefValue<T>(ref: Ref<T> | undefined, value: T | null) {
  if (typeof ref === "function") {
    ref(value);
  } else if (ref) {
    ref.current = value;
  }
}

export function Clouds({
  children,
  className,
  contentClassName,
  contentRef: forwardedContentRef,
  ...options
}: CloudsProps) {
  const contentRef = useRef<HTMLDivElement>(null);
  const outputRef = useRef<HTMLCanvasElement>(null);
  const instanceRef = useRef<CloudsInstance | null>(null);
  const [initialOptions] = useState(options);
  const setContentRef = useCallback(
    (element: HTMLDivElement | null) => {
      contentRef.current = element;
      setRefValue(forwardedContentRef, element);
    },
    [forwardedContentRef],
  );

  useEffect(() => {
    const content = contentRef.current;
    const output = outputRef.current;
    if (!content || !output) return;
    instanceRef.current = createClouds({ content, output }, initialOptions);
    return () => {
      instanceRef.current?.destroy();
      instanceRef.current = null;
    };
  }, [initialOptions]);

  useEffect(() => {
    instanceRef.current?.setOptions(options);
  });

  const contentStyle: CSSProperties = {
    position: "relative",
    width: "100%",
    height: "100%",
    overflow: "auto",
  };

  return (
    <div className={className} style={{ position: "relative" }}>
      <div ref={setContentRef} className={contentClassName} style={contentStyle}>
        {children}
      </div>
      <canvas
        ref={outputRef}
        aria-hidden
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          pointerEvents: "none",
          zIndex: 1,
        }}
      />
    </div>
  );
}
